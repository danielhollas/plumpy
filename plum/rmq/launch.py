import logging
import uuid
from functools import partial

import pika
import yaml

import plum
import plum.utils
from . import defaults
from . import messages
from . import pubsub
from . import utils

_LOGGER = logging.getLogger(__name__)

__all__ = ['RmqTaskSubscriber', 'RmqTaskPublisher']


class TaskMessage(messages.Message):
    @staticmethod
    def create_launch(process_class, init_args=None, init_kwargs=None, play=True):
        body = create_launch_body(process_class, init_args, init_kwargs, play)
        return TaskMessage(body)

    @staticmethod
    def create_continue(pid, tag=None, play=True):
        body = create_continue_body(pid, tag, play)
        return TaskMessage(body)

    def __init__(self, body, correlation_id=None):
        super(TaskMessage, self).__init__()
        self.correlation_id = correlation_id if correlation_id is not None else str(uuid.uuid4())
        self.body = body
        self.future = plum.Future()

    def send(self, publisher):
        if self.correlation_id is None:
            self.correlation_id = str(uuid.uuid4())
        publisher.publish_msg(self.body, None, self.correlation_id)
        return self.future

    def on_delivered(self, publisher):
        publisher.await_response(self.correlation_id, self.on_response)

    def on_delivery_failed(self, publisher, reason):
        self.future.set_exception(RuntimeError("Message could not be delivered: {}".format(reason)))

    def on_response(self, done_future):
        plum.copy_future(done_future, self.future)


class RmqTaskSubscriber(messages.BaseConnectionWithExchange):
    """
    Listens for tasks coming in on the RMQ task queue
    """

    def __init__(self, connector,
                 task_queue_name=defaults.TASK_QUEUE,
                 testing_mode=False,
                 decoder=yaml.load,
                 encoder=yaml.dump,
                 exchange_name=defaults.MESSAGE_EXCHANGE,
                 exchange_params=None,
                 ):
        """
        :param connector: An RMQ connector
        :type connector: :class:`pubsub.RmqConnector`
        :param task_queue_name: The name of the queue to use
        :param decoder: A message decoder
        :param encoder: A response encoder
        """
        super(RmqTaskSubscriber, self).__init__(
            connector,
            exchange_name=exchange_name,
            exchange_params=exchange_params
        )

        self._task_queue_name = task_queue_name
        self._testing_mode = testing_mode
        self._decode = decoder
        self._encode = encoder

        self._listeners = []

    def add_task_receiver(self, task_receiver):
        self._listeners.append(task_receiver)

    def remove_task_receiver(self, task_receiver):
        self._listeners.remove(task_receiver)

    @messages.initialiser()
    def on_channel_open(self, channel):
        super(RmqTaskSubscriber, self).on_channel_open(channel)
        channel.basic_qos(prefetch_count=1)

    @messages.initialiser()
    def on_exchange_declareok(self, unused_frame):
        super(RmqTaskSubscriber, self).on_exchange_declareok(unused_frame)
        self.get_channel().queue_declare(
            self._on_task_queue_declaredok, queue=self._task_queue_name,
            durable=not self._testing_mode, auto_delete=self._testing_mode)

    @messages.initialiser()
    def _on_task_queue_declaredok(self, frame):
        queue_name = frame.method.queue
        self.get_channel().queue_bind(
            self._on_task_queue_bindok, queue_name, self._exchange_name,
            routing_key=queue_name)

    @messages.initialiser()
    def _on_task_queue_bindok(self, unused_frame):
        self._consumer_tag = \
            self.get_channel().basic_consume(self._on_task, self._task_queue_name)

    def _on_task(self, ch, method, props, body):
        handled = False
        for receiver in self._listeners:
            try:
                task = self._decode(body)
                result = receiver.on_task_received(task)
                if isinstance(result, plum.Future):
                    result.add_done_callback(partial(self._on_task_done, props, method))
                else:
                    # Finished
                    self._task_finished(props, method, utils.result_response(result))
                handled = True
                break
            except plum.TaskRejected:
                pass
            except KeyboardInterrupt:
                raise
            except Exception as e:
                self._task_finished(props, method, utils.exception_response(e))
                handled = True

        if not handled:
            self._channel.basic_reject(delivery_tag=method.delivery_tag)

    def _on_task_done(self, props, method, future):
        try:
            response = utils.result_response(future.result())
        except Exception as e:
            response = utils.exception_response(e)
        self._task_finished(props, method, response)

    def _task_finished(self, props, method, response):
        """
        Send an acknowledgement of the task being actioned and a response to the
        initiator.

        :param props: The message properties
        :param method: The message method
        :param response: The response to send to the initiator
        """
        self._send_response(props.correlation_id, props.reply_to, response)
        self._channel.basic_ack(delivery_tag=method.delivery_tag)

    def _send_response(self, correlation_id, reply_to, response):
        # Build full response
        response[utils.HOST_KEY] = utils.get_host_info()
        self.get_channel().basic_publish(
            exchange='', routing_key=reply_to,
            body=self._encode(response),
            properties=pika.BasicProperties(correlation_id=correlation_id))


class RmqTaskPublisher(messages.BasePublisherWithReplyQueue):
    """
    Publishes messages to the RMQ task queue and gets the response
    """

    def __init__(self, connector,
                 task_queue_name=defaults.TASK_QUEUE,
                 testing_mode=False,
                 exchange_name=defaults.MESSAGE_EXCHANGE,
                 exchange_params=None,
                 encoder=yaml.dump,
                 decoder=yaml.load,
                 confirm_deliveries=True, ):
        super(RmqTaskPublisher, self).__init__(
            connector,
            exchange_name=exchange_name,
            exchange_params=exchange_params,
            encoder=encoder,
            decoder=decoder,
            confirm_deliveries=confirm_deliveries
        )
        self._task_queue_name = task_queue_name
        self._testing_mode = testing_mode

    @messages.initialiser()
    def on_exchange_declareok(self, frame):
        super(RmqTaskPublisher, self).on_exchange_declareok(frame)

        # The task queue
        self.get_channel().queue_declare(
            self._on_task_queue_declareok,
            self._task_queue_name, durable=not self._testing_mode,
            auto_delete=self._testing_mode)

    @messages.initialiser()
    def _on_task_queue_declareok(self, frame):
        queue_name = frame.method.queue
        self.get_channel().queue_bind(
            self._on_task_queue_bindok, queue_name, self._exchange_name,
            routing_key=queue_name)

    @messages.initialiser()
    def _on_task_queue_bindok(self, unused_frame):
        pass

    def publish_msg(self, task, routing_key, correlation_id):
        if routing_key is not None:
            _LOGGER.warn(
                "Routing key '{}' passed but is ignored for all tasks".format(routing_key))

        properties = pika.BasicProperties(
            reply_to=self.get_reply_queue_name(),
            delivery_mode=2,  # Persistent
            correlation_id=correlation_id
        )
        self._channel.basic_publish(
            exchange=self.get_exchange_name(),
            routing_key=self._task_queue_name,
            body=self._encode(task),
            properties=properties)

    def task_send(self, msg):
        message = TaskMessage(msg)
        self.action_message(message)
        return message.future