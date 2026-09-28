"""Kubernetes cluster backend.

Provides management of Kubernetes clusters including contexts, namespaces,
pods, deployments, services, ingresses, exec, logs, and scaling operations.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream

from vm_harness.container.backend import (
    CommandResult,
    ContainerBackend,
    ContainerConfig,
    ContainerImage,
    ContainerNetwork,
    ContainerStats,
    ContainerVolume,
)
from vm_harness.container.kubernetes.models import (
    ConfigMap,
    Deployment,
    Ingress,
    KubeCluster,
    KubeService,
    Namespace,
    Pod,
    Secret,
)
from vm_harness.container.kubernetes.watcher import KubernetesWatcher

logger = logging.getLogger(__name__)


class KubernetesError(Exception):
    """Base exception for Kubernetes backend errors."""

    def __init__(self, message: str, cause: Optional[Exception] = None):
        super().__init__(message)
        self.cause = cause


class NotFoundError(KubernetesError):
    """Raised when a resource is not found."""
    pass


class ConflictError(KubernetesError):
    """Raised when there is a naming conflict."""
    pass


class KubernetesBackend(ContainerBackend):
    """Kubernetes cluster backend.

    Manages Kubernetes resources across multiple contexts and namespaces,
    providing a unified interface for cluster operations.
    """

    def __init__(
        self,
        kubeconfig: Optional[str] = None,
        context: Optional[str] = None,
    ):
        """
        Args:
            kubeconfig: Path to kubeconfig file. None for default.
            context: Context to use. None for current context.
        """
        self._kubeconfig = kubeconfig
        self._context = context
        self._core_v1: Optional[client.CoreV1Api] = None
        self._apps_v1: Optional[client.AppsV1Api] = None
        self._networking_v1: Optional[client.NetworkingV1Api] = None
        self._watcher: Optional[KubernetesWatcher] = None
        self._connected = False

    # ── Connection management ──────────────────────────────────────────────────

    async def connect(self) -> None:
        """Initialize connection to Kubernetes cluster."""
        loop = asyncio.get_event_loop()

        def _connect():
            try:
                if self._kubeconfig:
                    config.load_kube_config(
                        config_file=self._kubeconfig,
                        context=self._context,
                    )
                else:
                    try:
                        config.load_kube_config(context=self._context)
                    except Exception:
                        config.load_incluster_config()

                # A cluster that is down must fail fast: the default client retries each call three times and
                # takes seconds to give up, which froze anything waiting on it (the GUI's container page did).
                cfg = client.Configuration.get_default_copy()
                cfg.retries = 0
                api = client.ApiClient(cfg)
                try:
                    client.VersionApi(api).get_code(_request_timeout=(3, 5))
                except Exception as e:  # noqa: BLE001
                    api.close()
                    raise KubernetesError(f"Kubernetes cluster not reachable ({cfg.host}): {type(e).__name__}", e)
                self._core_v1 = client.CoreV1Api(api)
                self._apps_v1 = client.AppsV1Api(api)
                self._networking_v1 = client.NetworkingV1Api(api)
                self._watcher = KubernetesWatcher(
                    self._core_v1,
                    self._apps_v1,
                    self._networking_v1,
                )
            except Exception as e:
                raise KubernetesError(f"Failed to connect to Kubernetes: {e}", e)

        await loop.run_in_executor(None, _connect)
        self._connected = True

    async def disconnect(self) -> None:
        """Close connection to Kubernetes cluster."""
        if self._watcher:
            await self._watcher.stop_all()
        if self._core_v1 and hasattr(self._core_v1, 'api_client'):
            self._core_v1.api_client.close()
        if self._apps_v1 and hasattr(self._apps_v1, 'api_client'):
            self._apps_v1.api_client.close()
        if self._networking_v1 and hasattr(self._networking_v1, 'api_client'):
            self._networking_v1.api_client.close()
        self._connected = False

    async def is_connected(self) -> bool:
        """Check if connected to Kubernetes cluster."""
        if not self._connected or not self._core_v1:
            return False
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._core_v1.get_api_resources(),
            )
            return True
        except Exception:
            return False

    def _ensure_connected(self) -> None:
        """Ensure client is connected."""
        if not self._connected or not self._core_v1:
            raise KubernetesError("Not connected to Kubernetes. Call connect() first.")

    # ── Context management ─────────────────────────────────────────────────────

    async def list_contexts(self) -> List[KubeCluster]:
        """List all contexts in kubeconfig."""
        loop = asyncio.get_event_loop()

        def _list():
            try:
                contexts, active_context = config.list_kube_config_contexts(
                    config_file=self._kubeconfig,
                )
                return [
                    KubeCluster.from_dict(ctx, active=ctx == active_context)
                    for ctx in contexts
                ]
            except Exception as e:
                raise KubernetesError(f"Failed to list contexts: {e}", e)

        return await loop.run_in_executor(None, _list)

    async def switch_context(self, context_name: str) -> None:
        """Switch to a different context.

        Args:
            context_name: Name of the context to switch to.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _switch():
            try:
                config.load_kube_config(
                    config_file=self._kubeconfig,
                    context=context_name,
                )
                self._core_v1 = client.CoreV1Api()
                self._apps_v1 = client.AppsV1Api()
                self._networking_v1 = client.NetworkingV1Api()
                self._watcher = KubernetesWatcher(
                    self._core_v1,
                    self._apps_v1,
                    self._networking_v1,
                )
            except Exception as e:
                raise KubernetesError(f"Failed to switch context: {e}", e)

        await loop.run_in_executor(None, _switch)

    # ── Namespace management ───────────────────────────────────────────────────

    async def list_namespaces(self) -> List[Namespace]:
        """List all namespaces."""
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _list():
            try:
                ns_list = self._core_v1.list_namespace()
                return [Namespace.from_dict(ns.to_dict()) for ns in ns_list.items]
            except ApiException as e:
                raise KubernetesError(f"Failed to list namespaces: {e}", e)

        return await loop.run_in_executor(None, _list)

    async def create_namespace(self, name: str) -> Namespace:
        """Create a new namespace.

        Args:
            name: Namespace name.

        Returns:
            Created Namespace.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _create():
            try:
                body = Namespace(name=name).to_dict()
                ns = self._core_v1.create_namespace(body=body)
                return Namespace.from_dict(ns.to_dict())
            except ApiException as e:
                if e.status == 409:
                    raise ConflictError(f"Namespace '{name}' already exists")
                raise KubernetesError(f"Failed to create namespace: {e}", e)

        return await loop.run_in_executor(None, _create)

    async def delete_namespace(self, name: str) -> None:
        """Delete a namespace.

        Args:
            name: Namespace name.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _delete():
            try:
                self._core_v1.delete_namespace(name=name)
            except ApiException as e:
                if e.status == 404:
                    raise NotFoundError(f"Namespace '{name}' not found")
                raise KubernetesError(f"Failed to delete namespace: {e}", e)

        await loop.run_in_executor(None, _delete)

    # ── Pod management ─────────────────────────────────────────────────────────

    async def list_pods(self, namespace: str = "default") -> List[Pod]:
        """List pods in a namespace.

        Args:
            namespace: Namespace to list pods from.

        Returns:
            List of Pod objects.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _list():
            try:
                pods = self._core_v1.list_namespaced_pod(namespace=namespace)
                return [Pod.from_dict(p.to_dict()) for p in pods.items]
            except ApiException as e:
                raise KubernetesError(f"Failed to list pods: {e}", e)

        return await loop.run_in_executor(None, _list)

    async def list_nodes(self) -> List[Any]:
        """List all Kubernetes nodes."""
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _list():
            try:
                nodes = self._core_v1.list_node()
                return [
                    {
                        "name": node.metadata.name,
                        "status": "Ready" if any(
                            condition.type == "Ready" and condition.status == "True"
                            for condition in (node.status.conditions or [])
                        ) else "NotReady",
                        "age": str(node.metadata.creation_timestamp),
                    }
                    for node in nodes.items
                ]
            except ApiException as e:
                raise KubernetesError(f"Failed to list nodes: {e}", e)

        return await loop.run_in_executor(None, _list)

    async def apply_manifest(self, manifest: dict) -> dict:
        """Apply a YAML manifest to the cluster."""
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _apply():
            import tempfile
            import yaml
            from kubernetes.utils import create_from_yaml
            try:
                api_client = self._core_v1.api_client
                yaml_str = yaml.dump(manifest)
                # create_from_yaml expects a file path
                with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                    f.write(yaml_str)
                    f.flush()
                    temp_path = f.name
                try:
                    create_from_yaml(api_client, temp_path)
                finally:
                    import os
                    os.unlink(temp_path)
                return {"status": "applied", "manifest": manifest.get("metadata", {}).get("name", "unknown")}
            except Exception as e:
                raise KubernetesError(f"Failed to apply manifest: {e}", e)

        return await loop.run_in_executor(None, _apply)

    async def delete_resource(self, kind: str, name: str, namespace: str = "default") -> dict:
        """Delete a Kubernetes resource."""
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _delete():
            try:
                if kind == "pod":
                    self._core_v1.delete_namespaced_pod(name=name, namespace=namespace)
                elif kind == "deployment":
                    self._apps_v1.delete_namespaced_deployment(name=name, namespace=namespace)
                elif kind == "service":
                    self._core_v1.delete_namespaced_service(name=name, namespace=namespace)
                else:
                    raise KubernetesError(f"Unsupported resource kind: {kind}")
                return {"status": "deleted", "kind": kind, "name": name}
            except ApiException as e:
                raise KubernetesError(f"Failed to delete {kind}/{name}: {e}", e)

        return await loop.run_in_executor(None, _delete)

    async def exec_in_pod(self, pod_name: str, namespace: str, command: list, timeout: int = 30) -> str:
        """Execute a command in a pod."""
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _exec():
            from kubernetes.stream import stream
            try:
                resp = stream(
                    self._core_v1.connect_get_namespaced_pod_exec,
                    pod_name, namespace,
                    command=command,
                    stderr=True, stdin=False,
                    stdout=True, tty=False,
                )
                return resp
            except ApiException as e:
                raise KubernetesError(f"Failed to exec in pod: {e}", e)

        return await loop.run_in_executor(None, _exec)

    async def get_pod_logs(self, pod_name: str, namespace: str, tail_lines: int = 100) -> str:
        """Get logs from a pod."""
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _logs():
            try:
                return self._core_v1.read_namespaced_pod_log(
                    pod_name, namespace, tail_lines=tail_lines
                )
            except ApiException as e:
                raise KubernetesError(f"Failed to get logs: {e}", e)

        return await loop.run_in_executor(None, _logs)

    async def list_all_pods(self) -> List[Pod]:
        """List pods across all namespaces."""
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _list():
            try:
                pods = self._core_v1.list_pod_for_all_namespaces()
                return [Pod.from_dict(p.to_dict()) for p in pods.items]
            except ApiException as e:
                raise KubernetesError(f"Failed to list pods: {e}", e)

        return await loop.run_in_executor(None, _list)

    async def get_pod(self, name: str, namespace: str = "default") -> Pod:
        """Get a specific pod.

        Args:
            name: Pod name.
            namespace: Pod namespace.

        Returns:
            Pod details.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _get():
            try:
                pod = self._core_v1.read_namespaced_pod(name=name, namespace=namespace)
                return Pod.from_dict(pod.to_dict())
            except ApiException as e:
                if e.status == 404:
                    raise NotFoundError(f"Pod '{name}' not found in namespace '{namespace}'")
                raise KubernetesError(f"Failed to get pod: {e}", e)

        return await loop.run_in_executor(None, _get)

    async def delete_pod(self, name: str, namespace: str = "default") -> None:
        """Delete a pod.

        Args:
            name: Pod name.
            namespace: Pod namespace.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _delete():
            try:
                self._core_v1.delete_namespaced_pod(name=name, namespace=namespace)
            except ApiException as e:
                if e.status == 404:
                    raise NotFoundError(f"Pod '{name}' not found in namespace '{namespace}'")
                raise KubernetesError(f"Failed to delete pod: {e}", e)

        await loop.run_in_executor(None, _delete)

    # ── Pod logs ───────────────────────────────────────────────────────────────

    async def get_pod_logs(
        self,
        name: str,
        namespace: str = "default",
        container: Optional[str] = None,
        tail: int = 100,
        since: Optional[str] = None,
        timestamps: bool = False,
        follow: bool = False,
    ) -> Union[str, AsyncIterator[str]]:
        """Get logs from a pod.

        Args:
            name: Pod name.
            namespace: Pod namespace.
            container: Container name (required for multi-container pods).
            tail: Number of lines from the end.
            since: Show logs since timestamp.
            timestamps: Include timestamps.
            follow: Stream logs.

        Returns:
            Log string, or AsyncIterator if follow=True.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _get_logs():
            try:
                kwargs: Dict[str, Any] = {
                    "tail_lines": tail,
                    "timestamps": timestamps,
                }
                if container:
                    kwargs["container"] = container
                if since:
                    kwargs["since_seconds"] = since

                if follow:
                    return self._stream_pod_logs(name, namespace, **kwargs)
                else:
                    return self._core_v1.read_namespaced_pod_log(
                        name=name,
                        namespace=namespace,
                        **kwargs,
                    )
            except ApiException as e:
                if e.status == 404:
                    raise NotFoundError(f"Pod '{name}' not found in namespace '{namespace}'")
                raise KubernetesError(f"Failed to get pod logs: {e}", e)

        try:
            result = await loop.run_in_executor(None, _get_logs)
            return result
        except NotFoundError:
            raise
        except KubernetesError:
            raise
        except Exception as e:
            raise KubernetesError(f"Failed to get pod logs: {e}", e)

    async def _stream_pod_logs(
        self,
        name: str,
        namespace: str,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream logs from a pod."""
        from kubernetes import watch as k8s_watch

        loop = asyncio.get_event_loop()

        def _stream():
            w = k8s_watch.Watch()
            try:
                return w.stream(
                    self._core_v1.read_namespaced_pod_log,
                    name=name,
                    namespace=namespace,
                    follow=True,
                    **kwargs,
                )
            except Exception as e:
                logger.error(f"Log stream error: {e}")
                raise

        log_stream = await loop.run_in_executor(None, _stream)
        for line in log_stream:
            yield line

    # ── Pod exec ───────────────────────────────────────────────────────────────

    async def exec_command(
        self,
        container_id: str,
        command: Union[str, List[str]],
        tty: bool = False,
        timeout: Optional[int] = None,
    ) -> CommandResult:
        """Execute a command in a pod container.

        Args:
            container_id: Pod name (format: "namespace/pod/container" or just pod name).
            command: Command to execute.
            tty: Allocate a pseudo-TTY.
            timeout: Execution timeout.

        Returns:
            CommandResult with stdout, stderr, and return code.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        # Parse container_id
        parts = container_id.split("/")
        if len(parts) == 3:
            namespace, pod, container = parts
        elif len(parts) == 2:
            namespace, pod = parts
            container = None
        else:
            namespace = "default"
            pod = container_id
            container = None

        def _exec():
            try:
                if isinstance(command, list):
                    cmd = command
                else:
                    cmd = command.split()

                kwargs: Dict[str, Any] = {
                    "container": container,
                    "command": cmd,
                    "stderr": True,
                    "stdin": False,
                    "stdout": True,
                    "tty": tty,
                }

                response = stream(
                    self._core_v1.connect_get_namespaced_pod_exec,
                    pod,
                    namespace,
                    **kwargs,
                )
                return CommandResult(
                    stdout=response,
                    stderr="",
                    returncode=0,
                )
            except ApiException as e:
                if e.status == 404:
                    raise NotFoundError(f"Pod '{pod}' not found in namespace '{namespace}'")
                raise KubernetesError(f"Failed to execute command: {e}", e)
            except Exception as e:
                raise KubernetesError(f"Failed to execute command: {e}", e)

        try:
            return await loop.run_in_executor(None, _exec)
        except (NotFoundError, KubernetesError):
            raise
        except Exception as e:
            raise KubernetesError(f"Failed to execute command: {e}", e)

    # ── Deployment management ──────────────────────────────────────────────────

    async def list_deployments(self, namespace: str = "default") -> List[Deployment]:
        """List deployments in a namespace.

        Args:
            namespace: Namespace to list deployments from.

        Returns:
            List of Deployment objects.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _list():
            try:
                deps = self._apps_v1.list_namespaced_deployment(namespace=namespace)
                return [Deployment.from_dict(d.to_dict()) for d in deps.items]
            except ApiException as e:
                raise KubernetesError(f"Failed to list deployments: {e}", e)

        return await loop.run_in_executor(None, _list)

    async def list_all_deployments(self) -> List[Deployment]:
        """List deployments across all namespaces."""
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _list():
            try:
                deps = self._apps_v1.list_deployment_for_all_namespaces()
                return [Deployment.from_dict(d.to_dict()) for d in deps.items]
            except ApiException as e:
                raise KubernetesError(f"Failed to list deployments: {e}", e)

        return await loop.run_in_executor(None, _list)

    async def get_deployment(
        self,
        name: str,
        namespace: str = "default",
    ) -> Deployment:
        """Get a specific deployment.

        Args:
            name: Deployment name.
            namespace: Deployment namespace.

        Returns:
            Deployment details.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _get():
            try:
                dep = self._apps_v1.read_namespaced_deployment(
                    name=name, namespace=namespace
                )
                return Deployment.from_dict(dep.to_dict())
            except ApiException as e:
                if e.status == 404:
                    raise NotFoundError(
                        f"Deployment '{name}' not found in namespace '{namespace}'"
                    )
                raise KubernetesError(f"Failed to get deployment: {e}", e)

        return await loop.run_in_executor(None, _get)

    async def delete_deployment(
        self,
        name: str,
        namespace: str = "default",
    ) -> None:
        """Delete a deployment.

        Args:
            name: Deployment name.
            namespace: Deployment namespace.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _delete():
            try:
                self._apps_v1.delete_namespaced_deployment(
                    name=name, namespace=namespace
                )
            except ApiException as e:
                if e.status == 404:
                    raise NotFoundError(
                        f"Deployment '{name}' not found in namespace '{namespace}'"
                    )
                raise KubernetesError(f"Failed to delete deployment: {e}", e)

        await loop.run_in_executor(None, _delete)

    async def scale_deployment(
        self,
        name: str,
        namespace: str = "default",
        replicas: int = 1,
    ) -> None:
        """Scale a deployment to N replicas.

        Args:
            name: Deployment name.
            namespace: Deployment namespace.
            replicas: Number of replicas.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _scale():
            try:
                body = {"spec": {"replicas": replicas}}
                self._apps_v1.patch_namespaced_deployment_scale(
                    name=name,
                    namespace=namespace,
                    body=body,
                )
            except ApiException as e:
                if e.status == 404:
                    raise NotFoundError(
                        f"Deployment '{name}' not found in namespace '{namespace}'"
                    )
                raise KubernetesError(f"Failed to scale deployment: {e}", e)

        await loop.run_in_executor(None, _scale)

    # ── Service management ─────────────────────────────────────────────────────

    async def list_services(self, namespace: str = "default") -> List[KubeService]:
        """List services in a namespace.

        Args:
            namespace: Namespace to list services from.

        Returns:
            List of KubeService objects.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _list():
            try:
                svcs = self._core_v1.list_namespaced_service(namespace=namespace)
                return [KubeService.from_dict(s.to_dict()) for s in svcs.items]
            except ApiException as e:
                raise KubernetesError(f"Failed to list services: {e}", e)

        return await loop.run_in_executor(None, _list)

    async def list_all_services(self) -> List[KubeService]:
        """List services across all namespaces."""
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _list():
            try:
                svcs = self._core_v1.list_service_for_all_namespaces()
                return [KubeService.from_dict(s.to_dict()) for s in svcs.items]
            except ApiException as e:
                raise KubernetesError(f"Failed to list services: {e}", e)

        return await loop.run_in_executor(None, _list)

    async def get_service(
        self,
        name: str,
        namespace: str = "default",
    ) -> KubeService:
        """Get a specific service.

        Args:
            name: Service name.
            namespace: Service namespace.

        Returns:
            KubeService details.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _get():
            try:
                svc = self._core_v1.read_namespaced_service(
                    name=name, namespace=namespace
                )
                return KubeService.from_dict(svc.to_dict())
            except ApiException as e:
                if e.status == 404:
                    raise NotFoundError(
                        f"Service '{name}' not found in namespace '{namespace}'"
                    )
                raise KubernetesError(f"Failed to get service: {e}", e)

        return await loop.run_in_executor(None, _get)

    async def delete_service(
        self,
        name: str,
        namespace: str = "default",
    ) -> None:
        """Delete a service.

        Args:
            name: Service name.
            namespace: Service namespace.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _delete():
            try:
                self._core_v1.delete_namespaced_service(
                    name=name, namespace=namespace
                )
            except ApiException as e:
                if e.status == 404:
                    raise NotFoundError(
                        f"Service '{name}' not found in namespace '{namespace}'"
                    )
                raise KubernetesError(f"Failed to delete service: {e}", e)

        await loop.run_in_executor(None, _delete)

    # ── Ingress management ─────────────────────────────────────────────────────

    async def list_ingresses(self, namespace: str = "default") -> List[Ingress]:
        """List ingresses in a namespace.

        Args:
            namespace: Namespace to list ingresses from.

        Returns:
            List of Ingress objects.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _list():
            try:
                ingresses = self._networking_v1.list_namespaced_ingress(namespace=namespace)
                return [Ingress.from_dict(i.to_dict()) for i in ingresses.items]
            except ApiException as e:
                raise KubernetesError(f"Failed to list ingresses: {e}", e)

        return await loop.run_in_executor(None, _list)

    async def list_all_ingresses(self) -> List[Ingress]:
        """List ingresses across all namespaces."""
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _list():
            try:
                ingresses = self._networking_v1.list_ingress_for_all_namespaces()
                return [Ingress.from_dict(i.to_dict()) for i in ingresses.items]
            except ApiException as e:
                raise KubernetesError(f"Failed to list ingresses: {e}", e)

        return await loop.run_in_executor(None, _list)

    async def get_ingress(
        self,
        name: str,
        namespace: str = "default",
    ) -> Ingress:
        """Get a specific ingress.

        Args:
            name: Ingress name.
            namespace: Ingress namespace.

        Returns:
            Ingress details.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _get():
            try:
                ing = self._networking_v1.read_namespaced_ingress(
                    name=name, namespace=namespace
                )
                return Ingress.from_dict(ing.to_dict())
            except ApiException as e:
                if e.status == 404:
                    raise NotFoundError(
                        f"Ingress '{name}' not found in namespace '{namespace}'"
                    )
                raise KubernetesError(f"Failed to get ingress: {e}", e)

        return await loop.run_in_executor(None, _get)

    async def delete_ingress(
        self,
        name: str,
        namespace: str = "default",
    ) -> None:
        """Delete an ingress.

        Args:
            name: Ingress name.
            namespace: Ingress namespace.
        """
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _delete():
            try:
                self._networking_v1.delete_namespaced_ingress(
                    name=name, namespace=namespace
                )
            except ApiException as e:
                if e.status == 404:
                    raise NotFoundError(
                        f"Ingress '{name}' not found in namespace '{namespace}'"
                    )
                raise KubernetesError(f"Failed to delete ingress: {e}", e)

        await loop.run_in_executor(None, _delete)

    # ── ConfigMap & Secret management ──────────────────────────────────────────

    async def list_configmaps(self, namespace: str = "default") -> List[ConfigMap]:
        """List ConfigMaps in a namespace."""
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _list():
            try:
                cms = self._core_v1.list_namespaced_config_map(namespace=namespace)
                return [ConfigMap.from_dict(cm.to_dict()) for cm in cms.items]
            except ApiException as e:
                raise KubernetesError(f"Failed to list ConfigMaps: {e}", e)

        return await loop.run_in_executor(None, _list)

    async def get_configmap(
        self,
        name: str,
        namespace: str = "default",
    ) -> ConfigMap:
        """Get a specific ConfigMap."""
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _get():
            try:
                cm = self._core_v1.read_namespaced_config_map(
                    name=name, namespace=namespace
                )
                return ConfigMap.from_dict(cm.to_dict())
            except ApiException as e:
                if e.status == 404:
                    raise NotFoundError(
                        f"ConfigMap '{name}' not found in namespace '{namespace}'"
                    )
                raise KubernetesError(f"Failed to get ConfigMap: {e}", e)

        return await loop.run_in_executor(None, _get)

    async def list_secrets(self, namespace: str = "default") -> List[Secret]:
        """List Secrets in a namespace."""
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _list():
            try:
                secrets = self._core_v1.list_namespaced_secret(namespace=namespace)
                return [Secret.from_dict(s.to_dict()) for s in secrets.items]
            except ApiException as e:
                raise KubernetesError(f"Failed to list Secrets: {e}", e)

        return await loop.run_in_executor(None, _list)

    async def get_secret(
        self,
        name: str,
        namespace: str = "default",
    ) -> Secret:
        """Get a specific Secret."""
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _get():
            try:
                s = self._core_v1.read_namespaced_secret(
                    name=name, namespace=namespace
                )
                return Secret.from_dict(s.to_dict())
            except ApiException as e:
                if e.status == 404:
                    raise NotFoundError(
                        f"Secret '{name}' not found in namespace '{namespace}'"
                    )
                raise KubernetesError(f"Failed to get Secret: {e}", e)

        return await loop.run_in_executor(None, _get)

    # ── Watcher access ─────────────────────────────────────────────────────────

    @property
    def watcher(self) -> Optional[KubernetesWatcher]:
        """Get the KubernetesWatcher instance."""
        return self._watcher

    # ── ContainerBackend interface (partial implementation) ────────────────────
    # Kubernetes doesn't map 1:1 to container runtime, so some methods
    # raise NotImplementedError or provide K8s-specific equivalents.

    async def list_containers(self, all: bool = True) -> List[Any]:
        """List containers (returns pods as container-like objects)."""
        return await self.list_all_pods()

    async def get_container(self, container_id: str) -> Any:
        """Get a container (returns pod)."""
        parts = container_id.split("/")
        if len(parts) >= 2:
            return await self.get_pod(parts[-2], parts[0])
        return await self.get_pod(parts[0])

    async def create_container(self, config: ContainerConfig) -> Any:
        """Create a container (creates a pod)."""
        raise NotImplementedError(
            "Use Kubernetes-native resource creation (deployments, pods) instead"
        )

    async def start_container(self, container_id: str) -> None:
        """Start a container (pods auto-start in K8s)."""
        pass  # Pods auto-start

    async def stop_container(self, container_id: str, timeout: int = 10) -> None:
        """Stop a container (deletes the pod)."""
        parts = container_id.split("/")
        if len(parts) >= 2:
            await self.delete_pod(parts[-2], parts[0])
        else:
            await self.delete_pod(parts[0])

    async def restart_container(self, container_id: str, timeout: int = 10) -> None:
        """Restart a container (deletes pod, deployment recreates it)."""
        await self.stop_container(container_id, timeout)

    async def remove_container(
        self,
        container_id: str,
        force: bool = False,
        volumes: bool = False,
    ) -> None:
        """Remove a container (deletes the pod)."""
        await self.stop_container(container_id)

    async def get_logs(
        self,
        container_id: str,
        tail: int = 100,
        since: Optional[str] = None,
        timestamps: bool = False,
        follow: bool = False,
    ) -> Union[str, AsyncIterator[str]]:
        """Get logs (delegates to pod logs)."""
        parts = container_id.split("/")
        if len(parts) == 3:
            return await self.get_pod_logs(
                name=parts[1],
                namespace=parts[0],
                container=parts[2],
                tail=tail,
                since=since,
                timestamps=timestamps,
                follow=follow,
            )
        elif len(parts) == 2:
            return await self.get_pod_logs(
                name=parts[1],
                namespace=parts[0],
                tail=tail,
                since=since,
                timestamps=timestamps,
                follow=follow,
            )
        else:
            return await self.get_pod_logs(
                name=parts[0],
                tail=tail,
                since=since,
                timestamps=timestamps,
                follow=follow,
            )

    async def get_stats(self, container_id: str) -> ContainerStats:
        """Get container stats (not directly supported, returns empty)."""
        return ContainerStats()

    async def list_images(self) -> List[ContainerImage]:
        """List images (not applicable in K8s)."""
        return []

    async def pull_image(self, image: str, tag: str = "latest") -> None:
        """Pull image (not applicable in K8s)."""
        pass

    async def remove_image(self, image_id: str, force: bool = False) -> None:
        """Remove image (not applicable in K8s)."""
        pass

    async def list_networks(self) -> List[ContainerNetwork]:
        """List networks (returns services as network-like objects)."""
        services = await self.list_all_services()
        return [
            ContainerNetwork(
                id=svc.name,
                name=svc.name,
                driver=svc.service_type.value,
            )
            for svc in services
        ]

    async def create_network(
        self,
        name: str,
        driver: str = "bridge",
        internal: bool = False,
    ) -> ContainerNetwork:
        """Create a network (creates a service)."""
        raise NotImplementedError("Use Kubernetes Service creation instead")

    async def list_volumes(self) -> List[ContainerVolume]:
        """List volumes (returns persistent volume claims)."""
        self._ensure_connected()
        loop = asyncio.get_event_loop()

        def _list():
            try:
                pvcs = self._core_v1.list_persistent_volume_claim_for_all_namespaces()
                return [
                    ContainerVolume(
                        name=pvc.metadata.name,
                        driver=pvc.spec.storage_class_name or "local",
                        mountpoint=pvc.spec.volume_name or "",
                    )
                    for pvc in pvcs.items
                ]
            except ApiException as e:
                raise KubernetesError(f"Failed to list volumes: {e}", e)

        return await loop.run_in_executor(None, _list)

    async def create_volume(
        self,
        name: str,
        driver: str = "local",
        labels: Optional[Dict[str, str]] = None,
    ) -> ContainerVolume:
        """Create a volume (creates a PVC)."""
        raise NotImplementedError("Use Kubernetes PVC creation instead")
