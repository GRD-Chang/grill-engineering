"""只读汇总一次 CI attempt 的成本；保留原质量核验，不设耗时门槛。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess


DEPENDENCIES = {
    "Set up Python", "Set up Python for private checks", "Install system dependencies",
    "Prepare verified SQLite runtime", "Install product and development tools",
    "Install E2E test dependency",
}
CHECKS = {
    "Detect changes", "Select checks", "Verify selection", "Test CI selection rules",
    "Test CI evidence rules", "Smoke test installed CLI", "Run sync tests",
    "Run E2E harness tests", "Run type checks", "Verify independent checks",
    "Test private-only CI selection rules", "Run private-only sync tests",
    "Run private-only E2E harness tests", "Verify private-only checks",
    "Record complete test shard",
}


def timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def seconds(start: str | None, end: str | None) -> float | None:
    first, last = timestamp(start), timestamp(end)
    if first is None or last is None or last < first:
        return None
    return round((last - first).total_seconds(), 3)


def step_phase(job: str, step: str) -> str:
    if step == "Set up job":
        return "cold_start"
    if step in DEPENDENCIES:
        return "dependencies"
    if step == "Run tests":
        return "tests" if job.startswith("product-tests (") else "checks"
    return "checks" if step in CHECKS else "other"


def summarize(run: dict, jobs: list[dict], observed_at: str) -> dict:
    rows = []
    for job in jobs:
        complete = job.get("status") == "completed"
        not_run = job.get("conclusion") == "skipped" and not job.get("runner_id")
        duration = 0.0 if not_run else seconds(
            job.get("started_at"), job.get("completed_at") if complete else observed_at,
        )
        steps = []
        for step in job.get("steps", []):
            skipped = step.get("conclusion") == "skipped"
            steps.append({
                "name": step["name"], "phase": step_phase(job["name"], step["name"]),
                "status": step.get("status"), "conclusion": step.get("conclusion"),
                "seconds": None if skipped else seconds(step.get("started_at"), step.get("completed_at")),
                "started_at": step.get("started_at"), "completed_at": step.get("completed_at"),
            })
        required = {"Set up job"}
        if job["name"].startswith("product-tests ("):
            required |= {"Set up Python", "Install system dependencies", "Prepare verified SQLite runtime",
                         "Install product and development tools", "Run tests", "Record complete test shard"}
        elif job["name"] == "quality":
            required |= {"Verify selection", "Run tests"}
        for name in sorted(required - {step["name"] for step in steps}):
            steps.append({"name": name, "phase": step_phase(job["name"], name), "status": "missing",
                          "conclusion": None, "seconds": None, "started_at": None, "completed_at": None})
        setup = next((step for step in steps if step["phase"] == "cold_start"
                      and step["conclusion"] != "skipped"), {})
        rows.append({
            "id": job["id"], "name": job["name"], "status": job.get("status"),
            "conclusion": job.get("conclusion"), "runner_seconds": duration,
            "runner_seconds_complete": not_run or (complete and duration is not None),
            "queue_seconds": None if not_run else seconds(job.get("created_at"), job.get("started_at")),
            "cold_start_seconds": None if not_run else seconds(job.get("started_at"), setup.get("completed_at")),
            "steps": steps,
        })
    all_complete = bool(rows) and all(row["runner_seconds_complete"] for row in rows)
    measured = [row["runner_seconds"] for row in rows if row["runner_seconds"] is not None]
    quality = next((row for row in rows if row["name"] == "quality"), {})
    gate = next((step for step in quality.get("steps", []) if step["name"] == "Run tests"), {})
    # created_at 在重跑时仍指向首次创建；不能把两次 attempt 间隔算作本轮排队。
    origin = run.get("created_at") if run["run_attempt"] == 1 else run.get("run_started_at")
    end = max((job["completed_at"] for job in jobs if job.get("completed_at")), default=None)
    terminal = run.get("status") == "completed"
    return {
        "run_id": run["id"], "attempt": run["run_attempt"], "head_sha": run.get("head_sha"),
        "url": run.get("html_url"), "status": run.get("status"), "conclusion": run.get("conclusion"),
        "observed_at": observed_at, "origin": origin,
        "wall_seconds": seconds(origin, end if terminal and all_complete else observed_at),
        "necessary_checks_seconds": seconds(origin, gate.get("completed_at"))
        if gate.get("status") == "completed" and gate.get("conclusion") != "skipped" else None,
        "necessary_checks_conclusion": gate.get("conclusion"),
        "runner_seconds": round(sum(measured), 3) if measured else None,
        "complete": terminal and all_complete,
        "jobs": rows,
    }


def display(value: float | None) -> str:
    return "缺失" if value is None else f"{value:.3f} s"


def markdown(report: dict) -> str:
    if "error" in report:
        return f"## CI 成本观测\n\n元数据缺失：{report['error']}。没有生成完整耗时结论。\n"
    scope = "作业时长元数据完整" if report["complete"] else "部分，runner 总量为当前已观测下界"
    wall_scope = "完整" if report["complete"] else "部分"
    lines = [
        "## CI 成本观测", "",
        f"Run {report['run_id']} / attempt {report['attempt']} / `{report['head_sha']}`；{scope}。",
        f"结果：{report['status']} / {report['conclusion'] or '未结束'}；采样：{report['observed_at']}。", "",
        f"全部必要检查关键路径：{display(report['necessary_checks_seconds'])}"
        f"（quality 汇总结果：{report['necessary_checks_conclusion'] or '缺失'}）。",
        f"作业墙钟（{wall_scope}）：{display(report['wall_seconds'])}；runner 总用量：{display(report['runner_seconds'])}。", "",
        "| 作业 | 结果 | 排队 | 冷启动 | runner 用量 |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for job in report["jobs"]:
        name = job["name"].replace("|", "\\|")
        state = job["conclusion"] or job["status"] or "缺失"
        suffix = "（部分）" if not job["runner_seconds_complete"] else ""
        lines.append(f"| {name} | {state} | {display(job['queue_seconds'])} | "
                     f"{display(job['cold_start_seconds'])} | {display(job['runner_seconds'])}{suffix} |")
    lines += ["", "| 作业 / 步骤 | 阶段 | 结果 | 用时 |", "| --- | --- | --- | ---: |"]
    for job in report["jobs"]:
        for step in job["steps"]:
            if step["phase"] not in ("dependencies", "tests", "checks"):
                continue
            name = f"{job['name']} / {step['name']}".replace("|", "\\|")
            lines.append(f"| {name} | {step['phase']} | {step['conclusion'] or step['status'] or '缺失'} | "
                         f"{display(step['seconds'])} |")
    lines += [
        "", "排队按 job created_at→started_at；平台内部的 runner 分配无法继续拆分。",
        "冷启动仅计 runner 可见的 started_at→Set up job 完成；依赖准备、测试及独立检查逐步列出。",
        "关键路径从本 attempt 起点到 quality 的 Run tests 完成，包含依赖准备和所有必要检查；失败耗时不代表验证通过。",
        "runner 用量为各 job 运行秒数之和，包含准备、诊断和清理，不是账单分钟；仅统计本 attempt，不累计此前重跑。",
        "重跑沿用的较早 attempt 成功作业不计入本轮成本；较早证据仍由现有 quality 门禁按原身份核验。",
        "取消/跳过/缺失步骤保留原结论；未产出的 JUnit、resources.txt 或完整分片证据不能由本表补足。",
        "测试 CPU/RSS 等仍读取 test-diagnostics-shard-* 中的 test-report；并行步骤用时之和不等于关键路径。",
        "quality 自身尚在运行时只能观测部分成本；run 结束后可用同一助手只读刷新最终值。",
        "若 workflow 整体取消使本汇总未运行，成本附件也可能缺失；不能把缺失报告解释为零成本。",
        "CI 不套用本地 300 秒预算，也不增加耗时门禁。", "",
    ]
    return "\n".join(lines)


def api(path: str) -> dict:
    # 直接继承 gh 的宿主登录或 CI GH_TOKEN，不读取、复制或另配凭据。
    result = subprocess.run(
        ["gh", "api", "--method", "GET", "-H", "Accept: application/vnd.github+json",
         "-H", "X-GitHub-Api-Version: 2022-11-28", path],
        capture_output=True, text=True, timeout=20, check=False,
    )
    if result.returncode:
        raise OSError(f"gh api 读取失败（exit {result.returncode}）：{result.stderr.strip()}")
    return json.loads(result.stdout)


def collect(repository: str, run_id: int, attempt: int | None) -> tuple[dict, list[dict]]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("无效 repository")
    base = f"/repos/{repository}/actions/runs/{run_id}"
    run = api(f"{base}/attempts/{attempt}" if attempt else base)
    if run["id"] != run_id or (attempt is not None and run["run_attempt"] != attempt):
        raise ValueError("CI 元数据身份不匹配")
    base += f"/attempts/{run['run_attempt']}/jobs"
    jobs = []
    for page in range(1, 11):
        result = api(f"{base}?per_page=100&page={page}")
        jobs.extend(result["jobs"])
        if len(jobs) >= result["total_count"]:
            if len(jobs) != result["total_count"] or len({job["id"] for job in jobs}) != len(jobs):
                raise ValueError("作业元数据数量不一致或重复")
            return run, jobs
        if not result["jobs"]:
            break
    raise ValueError("作业元数据分页不完整")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--run-id", type=int, default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--attempt", type=int, default=os.environ.get("GITHUB_RUN_ATTEMPT"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    observed = datetime.now(timezone.utc).isoformat()
    try:
        if not args.run_id:
            raise ValueError("缺少 run-id")
        run, jobs = collect(args.repository, args.run_id, args.attempt)
        report = summarize(run, jobs, observed)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        # 保留缺失原因并暴露观测故障；阶段取消/缺失则由有效元数据报告，不在这里报错。
        report = {"observed_at": observed, "error": str(error), "complete": False}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "costs.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = markdown(report)
    (args.output / "summary.md").write_text(summary, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(summary)
    print(summary)
    if "error" in report:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
