import json
import subprocess

name = "atlas-knowledge-demo-test-neo4j"
info = json.loads(subprocess.check_output(["docker", "inspect", name]))[0]
assert info["Config"]["Image"] == "neo4j:5.26-community", "Unexpected test container image"
assert info["HostConfig"]["PortBindings"].get("7687/tcp") == [{"HostIp": "127.0.0.1", "HostPort": "17694"}], "Unexpected test container binding"
assert "NEO4J_AUTH=none" in info["Config"]["Env"], "Unexpected test auth configuration"
print("Verified the dedicated disposable test container.")
