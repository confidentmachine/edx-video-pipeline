"""
Start Celery Worker
"""

from __future__ import absolute_import

import os
from celery import Celery
from VEDA.utils import get_config

try:
    from control.veda_deliver import VedaDelivery
except ImportError:
    from veda_deliver import VedaDelivery


auth_dict = get_config()

CEL_BROKER = 'amqp://{rabbitmq_user}:{rabbitmq_pass}@{rabbitmq_broker}:5672//'.format(
    rabbitmq_user=auth_dict['rabbitmq_user'],
    rabbitmq_pass=auth_dict['rabbitmq_pass'],
    rabbitmq_broker=auth_dict['rabbitmq_broker']
)

CEL_BACKEND = 'amqp://{rabbitmq_user}:{rabbitmq_pass}@{rabbitmq_broker}:5672//'.format(
    rabbitmq_user=auth_dict['rabbitmq_user'],
    rabbitmq_pass=auth_dict['rabbitmq_pass'],
    rabbitmq_broker=auth_dict['rabbitmq_broker']
)

app = Celery(auth_dict['celery_app_name'], broker=CEL_BROKER, backend=CEL_BACKEND, include=[])

app.conf.update(
    BROKER_CONNECTION_TIMEOUT=60,
    CELERY_IGNORE_RESULT=True,
    CELERY_TASK_RESULT_EXPIRES=10,
    CELERYD_PREFETCH_MULTIPLIER=1,
    CELERY_ACCEPT_CONTENT=['pickle', 'json', 'msgpack', 'yaml']
)


@app.task(name='worker_encode')
def worker_task_fire(veda_id, encode_profile, jobid):
    pass


@app.task(name='supervisor_deliver')
def deliverable_route(veda_id, encode_profile):
    """
    Task for deliverable route.
    """
    veda_deliver = VedaDelivery(
        veda_id=veda_id,
        encode_profile=encode_profile
    )
    veda_deliver.run()


@app.task
def node_test(command):
    os.system(command)


@app.task(name='legacy_heal')
def maintainer_healer(command):
    os.system(command)


if __name__ == '__main__':
    app.start()
