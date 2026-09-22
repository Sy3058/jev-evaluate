"""Optional Docker runner for registered Python function test cases.

No host fallback, image pulls, shell interpolation, or privileged containers.
Only a preinstalled local image is used, by its immutable image ID.
"""
import json
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

RUNNER = '''import contextlib, io, json, runpy
spec=json.load(open('/work/tests.json'))
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    module=runpy.run_path('/work/solution.py')
results=[]
for test in spec['cases']:
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            actual=module[spec['function']](*test['args'])
        results.append({'passed':actual==test['expected'],'actual':repr(actual)[:500]})
    except BaseException as e:
        results.append({'passed':False,'error':type(e).__name__})
print(json.dumps(results))
'''


def bounded_run(command: list[str]) -> subprocess.CompletedProcess:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()

    def read(stream, target):
        with stream:
            while chunk := stream.read(8192):
                if len(target) + len(chunk) > 65536:
                    overflow.set()
                    process.kill()
                    break
                target.extend(chunk)

    threads = [threading.Thread(target=read, args=(stream, buf), daemon=True)
               for stream, buf in zip((process.stdout, process.stderr), buffers)]
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        for thread in threads:
            thread.join(timeout=2)
    if overflow.is_set():
        raise ValueError("실행 출력이 64 KiB 제한을 초과했습니다.")
    return subprocess.CompletedProcess(command, process.returncode, bytes(buffers[0]), bytes(buffers[1]))


def run_python(source: str, spec: dict) -> dict:
    if not spec:
        return {"status": "not_run", "reason": "등록된 함수 테스트 없음"}
    docker = shutil.which("docker")
    if not docker:
        return {"status": "unavailable", "reason": "Docker 실행 환경 없음. 호스트 실행으로 대체하지 않습니다."}
    image = "python:3.12-slim"
    try:
        inspected = subprocess.run([docker, "image", "inspect", image, "--format", "{{.Id}}"],
                                   capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "unavailable", "reason": str(error)[:200]}
    image_id = inspected.stdout.strip()
    if inspected.returncode or not image_id.startswith("sha256:"):
        return {"status": "unavailable", "reason": "로컬 python:3.12-slim 이미지 또는 Docker 데몬 없음. 자동 다운로드하지 않습니다."}
    name = "jev-test-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="jev-code-") as directory:
        work = Path(directory)
        work.chmod(0o755)
        (work / "solution.py").write_text(source, encoding="utf-8")
        (work / "tests.json").write_text(json.dumps(spec), encoding="utf-8")
        (work / "runner.py").write_text(RUNNER, encoding="utf-8")
        command = [docker, "run", "--rm", "--name", name, "--network", "none", "--read-only", "--log-driver", "none",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64",
                   "--memory", "128m", "--cpus", "0.5", "--user", "65534:65534",
                   "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m", "--mount", f"type=bind,src={work},dst=/work,readonly",
                   image_id, "python", "-I", "-B", "/work/runner.py"]
        try:
            process = bounded_run(command)
            if process.returncode:
                return {"status": "failed", "imageId": image_id,
                        "reason": process.stderr.decode("utf-8", errors="replace")[-1000:]}
            results = json.loads(process.stdout.decode("utf-8"))
            if not isinstance(results, list) or len(results) != len(spec["cases"]) or any(type(r.get("passed")) is not bool for r in results):
                raise ValueError("잘못된 테스트 실행 결과")
            return {"status": "executed", "imageId": image_id, "results": results,
                    "passed": all(r["passed"] for r in results),
                    "scope": "등록된 테스트만 실행. 제출 코드가 실행 환경/결과를 조작하지 않았다는 보장은 아님."}
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "imageId": image_id, "reason": "15초 실행 제한 초과"}
        except (OSError, ValueError, TypeError, AttributeError) as error:
            return {"status": "failed", "imageId": image_id, "reason": str(error)[:300]}
        finally:
            # Only this invocation's randomly named container; no broad cleanup.
            try:
                subprocess.run([docker, "rm", "--force", name], capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
