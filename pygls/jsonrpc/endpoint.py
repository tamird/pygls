##########################################################################
# Original work Copyright 2018 Palantir Technologies, Inc.               #
# Original work licensed under the MIT License.                          #
# See ThirdPartyNotices.txt in the project root for license information. #
# All modifications Copyright (c) Open Law Library. All rights reserved. #
##########################################################################
import logging
import sys
import uuid

from concurrent import futures
from .exceptions import JsonRpcException, JsonRpcRequestCancelled, \
    JsonRpcInternalError, JsonRpcMethodNotFound

CANCEL_METHOD = '$/cancelRequest'
JSONRPC_VERSION = '2.0'

log = logging.getLogger(__name__)


class Endpoint(object):

    def __init__(
        self,
        dispatcher,
        consumer,
        id_generator=lambda: str(uuid.uuid4()),
        max_workers=5
    ):
        """A JSON RPC endpoint for managing messages sent to/from the client.

        Args:
            dispatcher (dict): A dictionary of method name to handler function.
                The handler functions should return either the result
                or a callable that will be used to asynchronously
                compute the result.
            consumer (fn): A function that consumes JSON RPC message dicts and
                sends them to the client.
            id_generator (fn, optional): A function used to generate
                request IDs.
                Defaults to the string value of :func:`uuid.uuid4`.
            max_workers (int, optional): The number of workers in the
                asynchronous executor pool.
        """
        self._dispatcher = dispatcher
        self._consumer = consumer
        self._id_generator = id_generator

        self._client_request_futures = {}
        self._server_request_futures = {}
        self._executor_service = futures.ThreadPoolExecutor(
            max_workers=max_workers)

    def _cancel_callback(self, request_id):
        """Construct a cancellation callback for the given request ID."""
        def callback(future):
            if future.cancelled():
                self.notify(CANCEL_METHOD, {'id': request_id})
                future.set_exception(JsonRpcRequestCancelled())
        return callback

    def _handle_cancel_notification(self, msg_id):
        """Handle a cancel notification from the client."""
        request_future = self._client_request_futures.pop(msg_id, None)

        if not request_future:
            log.warn(
                f"Cancel notification for unknown message id {msg_id}")
            return

        # Will only work if the request hasn't started executing
        if request_future.cancel():
            log.debug(f"Cancelled request with id {msg_id}")

    def _handle_notification(self, method, params):
        """Handle a notification from the client."""
        if method == CANCEL_METHOD:
            self._handle_cancel_notification(params['id'])
            return

        try:
            handler = self._dispatcher[method]
        except KeyError:
            log.warn(f"Ignoring notification for unknown method {method}")
            return

        try:
            handler_result = handler(params)
        except Exception:  # pylint: disable=broad-except
            log.exception(f"Failed to handle notification {method}: {params}")
            return

        if callable(handler_result):
            log.debug(f"Executing async notification handler {handler_result}")
            notification_future = self._executor_service.submit(handler_result)
            notification_future.add_done_callback(
                self._notification_callback(method, params))

    def _handle_request(self, msg_id, method, params):
        """Handle a request from the client."""
        try:
            handler = self._dispatcher[method]
        except KeyError:
            raise JsonRpcMethodNotFound.of(method)

        handler_result = handler(params)

        if callable(handler_result):
            log.debug(f"Executing async request handler {handler_result}")
            request_future = self._executor_service.submit(handler_result)
            self._client_request_futures[msg_id] = request_future
            request_future.add_done_callback(self._request_callback(msg_id))
        else:
            log.debug(
                f"Result from synchronous request handler: {handler_result}")
            self._consumer({
                'jsonrpc': JSONRPC_VERSION,
                'id': msg_id,
                'result': handler_result
            })

    def _handle_response(self, msg_id, result=None, error=None):
        """Handle a response from the client."""
        request_future = self._server_request_futures.pop(msg_id, None)

        if not request_future:
            log.warn(f"Received response to unknown message id {msg_id}")
            return

        if error is not None:
            log.debug(f"Received error response to message {msg_id}: {error}")
            request_future.set_exception(JsonRpcException.from_dict(error))

        log.debug(f"Received result for message {msg_id}: {result}")
        request_future.set_result(result)

    @staticmethod
    def _notification_callback(method, params):
        """Construct a notification callback for the given request ID."""
        def callback(future):
            try:
                future.result()
                log.debug(
                    f"Successfully handled async notification {method} \
                    {params}")
            except Exception:  # pylint: disable=broad-except
                log.exception(
                    f"Failed to handle async notification {method} {params}")
        return callback

    def _request_callback(self, request_id):
        """Construct a request callback for the given request ID."""
        def callback(future):
            # Remove the future from the client requests map
            self._client_request_futures.pop(request_id, None)

            if future.cancelled():
                future.set_exception(JsonRpcRequestCancelled())

            message = {
                'jsonrpc': JSONRPC_VERSION,
                'id': request_id,
            }

            try:
                message['result'] = future.result()
            except JsonRpcException as e:
                log.exception(f"Failed to handle request {request_id}")
                message['error'] = e.to_dict()
            except Exception:  # pylint: disable=broad-except
                log.exception(f"Failed to handle request {request_id}")
                message['error'] = JsonRpcInternalError.of(
                    sys.exc_info()).to_dict()

            self._consumer(message)

        return callback

    def consume(self, message):
        """Consume a JSON RPC message from the client.

        Args:
            message (dict): The JSON RPC message sent by the client
        """
        if 'jsonrpc' not in message or message['jsonrpc'] != JSONRPC_VERSION:
            log.warn(f"Unknown message type {message}")
            return

        if 'id' not in message:
            log.debug(f"Handling notification from client {message}")
            self._handle_notification(message['method'], message.get('params'))
        elif 'method' not in message:
            log.debug(f"Handling response from client {message}")
            self._handle_response(message['id'], message.get(
                'result'), message.get('error'))
        else:
            try:
                log.debug(f"Handling request from client {message}")
                self._handle_request(
                    message['id'], message['method'], message.get('params'))
            except JsonRpcException as e:
                log.exception(f"Failed to handle request {message['id']}")
                self._consumer({
                    'jsonrpc': JSONRPC_VERSION,
                    'id': message['id'],
                    'error': e.to_dict()
                })
            except Exception:  # pylint: disable=broad-except
                log.exception(f"Failed to handle request {message['id']}")
                self._consumer({
                    'jsonrpc': JSONRPC_VERSION,
                    'id': message['id'],
                    'error': JsonRpcInternalError.of(sys.exc_info()).to_dict()
                })

    def notify(self, method, params=None):
        """Send a JSON RPC notification to the client.

         Args:
             method (str): The method name of the notification to send
             params (any): The payload of the notification
         """
        log.debug(f'Sending notification: {method} {params}')

        message = {
            'jsonrpc': JSONRPC_VERSION,
            'method': method,
        }
        if params is not None:
            message['params'] = params

        self._consumer(message)

    def request(self, method, params=None):
        """Send a JSON RPC request to the client.

        Args:
            method (str): The method name of the message to send
            params (any): The payload of the message

        Returns:
            Future that will resolve once a response has been received
        """
        msg_id = self._id_generator()
        log.debug(f'Sending request with id {msg_id}: {method} {params}')

        message = {
            'jsonrpc': JSONRPC_VERSION,
            'id': msg_id,
            'method': method,
        }
        if params is not None:
            message['params'] = params

        request_future = futures.Future()
        request_future.add_done_callback(self._cancel_callback(msg_id))

        self._server_request_futures[msg_id] = request_future
        self._consumer(message)

        return request_future

    def shutdown(self):
        self._executor_service.shutdown()