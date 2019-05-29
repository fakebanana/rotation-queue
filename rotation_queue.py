#!/usr/bin/env python

### Only needs to run on master nodes (nodes running self-hosted control plane elements) ###

"""Use a resource annotation as a distributed lock to keep cert rotation of a service from happening at the same time"""
import os
import sys
import time

import logging
from pprint import pprint
import yaml
from kubernetes import client, config
from flask import Flask, Response, request, jsonify
from prometheus_client import Counter, Histogram, generate_latest

REQUEST_COUNT = Counter(
    'request_count', 'App Request Count',
    ['app_name', 'method', 'endpoint', 'http_status']
)
REQUEST_LATENCY = Histogram(
    'request_latency_seconds', 'Request latency',
    ['app_name', 'endpoint']
)

CONTENT_TYPE_LATEST = str('text/plain; version=0.0.4; charset=utf-8')

RESOURCE_TYPES = yaml.load("""
kube-apiserver: daemonset
kube-scheduler: deployment
kube-controller-manager: deployment
kube-proxy: daemonset
""")

CERT_TO_RESOURCE = yaml.load("""
/usr/local/akamai/kubernetes/certs/api/client_ca: kube-apiserver
/usr/local/akamai/kubernetes/certs/api/cluster_ca: kube-apiserver
/usr/local/akamai/kubernetes/certs/api/front_proxy.certificate: kube-apiserver
/usr/local/akamai/kubernetes/certs/api/api_server.certificate: kube-apiserver
/usr/local/akamai/kubernetes/certs/controller/cluster_ca: kube-controller-manager
/usr/local/akamai/kubernetes/certs/controller/controller.certificate: kube-controller-manager
/usr/local/akamai/kubernetes/certs/proxy/cluster_ca: kube-proxy
/usr/local/akamai/kubernetes/certs/proxy/kubeproxy.private_key: kube-proxy
/usr/local/akamai/kubernetes/certs/proxy/kubeproxy.certificate: kube-proxy
/usr/local/akamai/kubernetes/certs/scheduler/cluster_ca: kube-scheduler
/usr/local/akamai/kubernetes/certs/scheduler/scheduler.chain_cert: kube-scheduler
/usr/local/akamai/kubernetes/certs/scheduler/scheduler.certificate: kube-scheduler
""")

namespace = "kube-system"
nodename = os.environ['K8S_NODE']
annotation_key = "rotation-in-progress"

app = Flask(__name__)

def start_timer():
    request.start_time = time.time()

def stop_timer(response):
    resp_time = time.time() - request.start_time
    REQUEST_LATENCY.labels('rotation_queue', request.path).observe(resp_time)
    return response

def record_request_data(response):
    REQUEST_COUNT.labels('rotation_queue', request.method, request.path,
            response.status_code).inc()
    return response

def setup_metrics():
    app.before_request(start_timer)
    # The order here matters since we want stop_timer
    # to be executed first
    app.after_request(record_request_data)
    app.after_request(stop_timer)

def _resource_name(cert_name):
    try:
        return CERT_TO_RESOURCE[cert_name]
    except KeyError:
        logging.error("I don't know which service %s belongs to", cert_name)
        raise Exception()

def _resource_type(resource):
    try:
        return RESOURCE_TYPES[resource]
    except KeyError:
        logging.error("I don't resourcetype of %s", resource)
        raise Exception()

def _check_annotation(resource):
    api_instance=client.AppsV1Api()
    resource_type = _resource_type(resource)
    if resource_type == "daemonset":
        api_response = api_instance.read_namespaced_daemon_set(
            resource, namespace)
    elif resource_type == "deployment":
        api_response = api_instance.read_namespaced_deployment(
            resource, namespace)
    else:
        logging.error("Logic error: unexpected ressource_type %s", resource_type)
        raise Exception()
    try:
        return api_response.metadata.annotations[annotation_key]
    except KeyError:
        return False

def _patch_annotation(resource, patch):
    api_instance=client.AppsV1Api()

    resource_type = _resource_type(resource)
    if resource_type == "daemonset":
        api_response = api_instance.patch_namespaced_daemon_set(
            resource, namespace, patch)
    elif resource_type == "deployment":
        api_response = api_instance.patch_namespaced_deployment(
            resource, namespace, patch)
    else:
        logging.error("Logic error: unexpected resource_type %s", resource_type)
        raise Exception()

    return api_response

def create_rotation_lock(cert_name):
    """ create rotation lock by annotating service with name of name where rotation of service is taking place """

    patch={'metadata': {'annotations': {annotation_key: nodename}}}
    _response = _patch_annotation(_resource_name(cert_name), patch)
    return _check_annotation(_resource_name(cert_name))

def remove_rotation_lock(cert_name):
    """ remove rotation lock using json patch with positional arrays """
    patch = [{"op": "remove", "path": "/metadata/annotations/%s" % annotation_key}]
    try:
        _patch_annotation(_resource_name(cert_name), patch)
    except Exception:
        logging.error("Unexpected error removing rotation lock")
        raise

@app.route('/rotate', methods=['POST'])
def rotate():
    """
    If another node where a service is running is rotating the service certs,
    then block until that rotation has completed
    """
    cert_name = request.form['cert_name']
    while True:
        rotation_lock = _check_annotation(_resource_name(cert_name))
        if rotation_lock == nodename:  # already locked
            return "locked"
        elif rotation_lock:  # locked by something else
            continue
        else:  # not locked
            break

    result = create_rotation_lock(cert_name)

    if result != nodename:
        logging.error("Unexpectedly got %s after trying to lock %s for %s", result, _resource_name(cert_name), nodename)
        raise Exception()
    return "locked"

@app.route('/done', methods=['POST'])
def done():
    cert_name = request.form['cert_name']
    rotation_lock = _check_annotation(_resource_name(cert_name))
    if not rotation_lock:
        return "unlocked"
    elif rotation_lock != nodename:
        logging.error("Logic error: lock for %s is held by %s, but unlock request from %s",
            _resource_name(cert_name), rotation_lock, nodename)
        return "locked"
    else:
        remove_rotation_lock(cert_name)
    return "unlocked"

@app.route('/healthz')
def healthz():
    return ''

@app.errorhandler(500)
def handle_500(error):
    return str(error), 500

@app.route('/demo')
def demo():
    v1 = client.CoreV1Api()
    print("Listing pods with their IPs:")
    ret = v1.list_pod_for_all_namespaces(watch=False)
    response = "\n".join(["%s\t%s\t%s" % (i.status.pod_ip, i.metadata.namespace, i.metadata.name) for i in ret.items]) + "\n"
    return response

@app.route('/metrics')
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

if __name__ == '__main__':
    setup_metrics()
    config.load_incluster_config()
    app.run(host="0.0.0.0", port="8080")
