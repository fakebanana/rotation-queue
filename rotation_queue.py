#!/usr/bin/env python

### Only needs to run on master nodes (nodes running self-hosted control plane elements) ###

"""Use a resource annotation as a distributed lock to keep cert rotation of a service from happening at the same time"""
import os
import logging
from pprint import pprint
import yaml
from kubernetes import client, config
from klein import Klein
from twisted.internet import reactor
app = Klein()

"""
 destination='/usr/local/akamai/kubernetes/certs/api/client_ca'
 destination='/usr/local/akamai/kubernetes/certs/api/cluster_ca'
 destination='/usr/local/akamai/kubernetes/certs/api/front_proxy.certificate'
 destination='/usr/local/akamai/kubernetes/certs/controller/cluster_ca'
 destination='/usr/local/akamai/kubernetes/certs/controller/controller.certificate'
 destination='/usr/local/akamai/kubernetes/certs/node/cluster_ca'
 destination='/usr/local/akamai/kubernetes/certs/node/node.chain_cert'
 destination='/usr/local/akamai/kubernetes/certs/proxy/cluster_ca'
 destination='/usr/local/akamai/kubernetes/certs/proxy/kubeproxy.private_key'
 destination='/usr/local/akamai/kubernetes/certs/scheduler/cluster_ca'
 destination='/usr/local/akamai/kubernetes/certs/scheduler/scheduler.chain_cert'
"""

resource_types = yaml.load("""
kube-apiserver: daemonset
kube-scheduler: deployment
kube-controller-manager: deployment
kube-proxy: daemonset
""")

cert_to_resource = yaml.load("""
api/cluster_ca: kube-apiserver
api/client_ca: kube-apiserver
front_proxy.certificate: kube-apiserver
scheduler.certificate: kube-scheduler
controller/cluster_ca:  kube-controller-manager
controller.certificate: kube-controller-manager
api_server.certificate: kube-apiserver
kubeproxy.certificate: kube-proxy
""")

namespace = "kube-system"
nodename = os.environ['K8S_NODE']
annotation_key = "rotation-in-progress"

config.load_incluster_config()

def _resource_name(cert_name):
    try:
        return cert_to_resource[cert_name]
    except KeyError:
        logging.error("I don't know which service %s belongs to", cert_name)
        raise Exception()

def _resource_type(resource):
    try:
        return resource_types[resource]
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
            resource, namespace, patch, field_manager=nodename)
    elif resource_type == "deployment":
        api_response = api_instance.patch_namespaced_deployment(
            resource, namespace, patch, field_manager=nodename)
    else:
        logging.error("Logic error: unexpected resource_type %s", resource_type)
        raise Exception()

    return api_response

def create_rotation_lock(cert_name):
    """ create rotation lock by annotating service with name of name where rotation of service is taking place """

    patch={'metadata': {'annotations': {annotation_key: nodename}}}
    response = _patch_annotation(_resource_name(cert_name), patch)
    return _check_annotation(_resource_name(cert_name))

def remove_rotation_lock(cert_name):
    """ remove rotation lock using json patch with positional arrays """
    patch = [{"op": "remove", "path": "/metadata/annotations/%s" % annotation_key}]
    try:
        _patch_annotation(_resource_name(cert_name), patch)
    except Exception:
        logging.error("Unexpected error removing rotation lock")
        raise

@app.route('/healthz')
def healthz(_request):
    return ''

@app.route('/rotate/<string:cert_name>')
def rotate(_request, cert_name):
    """
    If another node where a service is running is rotating the service certs,
    then block until that rotation has completed
    """
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
        logging.error("Unexpectedly got %s after trying to lock %s for %s", result, _resource_name(cert_name))
        raise Exception()
    return "locked"

@app.route('/demo')
def demo(_request):
    v1 = client.CoreV1Api()
    print("Listing pods with their IPs:")
    ret = v1.list_pod_for_all_namespaces(watch=False)
    return "\n".join(["%s\t%s\t%s" % (i.status.pod_ip, i.metadata.namespace, i.metadata.name) for i in ret.items]) + "\n"

@app.route('/done/<string:cert_name>')
def done(_request, cert_name):
    if not _check_annotation(_resource_name(cert_name)):
        return "unlocked"
    remove_rotation_lock(cert_name)
    return "unlocked"

app.run("0.0.0.0", 8080)
