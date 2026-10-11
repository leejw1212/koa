"""클러스터 목록 읽기 — kubernetes MCP 또는 읽기 전용 kubectl. flowmap.py 가 쓴다. get/list 만 보낸다."""
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover import DEFAULT_KUBECONFIG, Kube  # noqa: E402
from paths import CATALOG, CONFIG  # noqa: E402

try:
    YLoader = yaml.CSafeLoader
except AttributeError:  # libyaml 없는 환경
    YLoader = yaml.SafeLoader


class McpSource:
    """kubernetes MCP 의 kubectl_get 으로 목록을 받는다.
    output=json 은 MCP 가 이름·상태만 남기고 줄이므로(mcp-server-kubernetes 4.1.9) yaml 로 받아 전체 필드를 쓴다.
    MCP 기본 출력 한도(SPAWN_MAX_BUFFER 1MB)는 큰 클러스터에서 넘으므로 이 프로세스에서만 올린다 (등록 설정은 그대로)."""
    label = "kubernetes MCP"

    def __init__(self, timeout):
        from mcp_client import Server, registered
        spec = registered().get("kubernetes") if CONFIG.is_file() else None
        self.registered = bool(spec)
        if not spec:
            spec = yaml.safe_load(CATALOG.read_text())["mcp_servers"]["kubernetes"]["server"]
        spec = dict(spec)
        spec["env"] = dict(spec.get("env") or {}, SPAWN_MAX_BUFFER=str(256 * 1024 * 1024))
        self.kubeconfig = os.path.expanduser(str(spec["env"].get("KUBECONFIG_PATH", DEFAULT_KUBECONFIG))
                                             .replace("${userHome}", str(Path.home())))
        self.server = Server("kubernetes", spec, timeout=timeout)
        self.timeout = timeout

    def __enter__(self):
        self.server.__enter__()
        return self

    def __exit__(self, *exc):
        return self.server.__exit__(*exc)

    def list(self, resource, ns=None):
        args = {"resourceType": resource, "output": "yaml"}
        if ns:
            args["namespace"] = ns
        elif resource != "events":
            args["allNamespaces"] = True
        err, text = self.server.call("kubectl_get", args, timeout=self.timeout)
        if err:
            raise RuntimeError(text.strip()[:200])
        doc = yaml.load(text, Loader=YLoader) or {}
        return doc.get("items") or []


class KubectlSource:
    label = "kubectl (읽기 전용 kubeconfig)"
    registered = False

    def __init__(self, kubeconfig, context):
        if Path(kubeconfig).expanduser().resolve() == (Path.home() / ".kube" / "config").resolve():
            raise SystemExit("~/.kube/config(관리자)로는 실행하지 않는다. 읽기 전용 kubeconfig 를 쓴다.")
        self.kubeconfig = kubeconfig
        self.kube = Kube(kubeconfig, context)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list(self, resource, ns=None):
        extra = ["--show-managed-fields"] if resource in ("configmaps", "networkpolicies.networking.k8s.io") else []
        return self.kube.json("get", resource, *(["-n", ns] if ns else ["-A"]), *extra).get("items") or []
