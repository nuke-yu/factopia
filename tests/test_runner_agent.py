"""runner 的 Agent 生命周期测试；只在 rdagent 镜像中可导入（app 镜像没有 RD-Agent 依赖）。

运行：
    docker compose -f docker-compose.yml -f compose.local.yml run --rm -T \
      -w /workspace -v "D:/sandbox/projects/factopia/tests:/workspace/tests:ro" \
      --entrypoint "" runner python -m unittest tests.test_runner_agent -v
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from factopia import runner
    from factopia.agents import children
    from factopia.agents import store as astore
    from factopia.research import store as rstore
except Exception:                       # app 镜像没有 rdagent：跳过
    runner = None


POLICY = {"max_rounds": 1, "budget_usd": 2.0, "no_improve_patience": 2,
          "human_gate": "costly_actions", "tools": ["new_version", "stop"],
          "costly_tools": ["new_version"], "child_loops": 1, "child_budget_usd": 0.1,
          "prompts": {"critic": "iteration-critic-v1", "planner": "iteration-planner-v1",
                      "evaluator": "iteration-evaluator-v1"}}


@unittest.skipIf(runner is None, "runner requires the rdagent image")
class RunnerAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        (self.runtime / "jobs").mkdir(parents=True)
        (self.runtime / "results" / "runs").mkdir(parents=True)
        self.patches = [
            mock.patch.object(astore, "AGENTS_DIR", self.runtime / "agents"),
            mock.patch.object(rstore, "RESEARCH_DIR", self.runtime / "research"),
            mock.patch.object(rstore, "JOBS_DIR", self.runtime / "jobs"),
            mock.patch.object(children, "RESULTS", self.runtime / "results"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_run_agent_syncs_stopped_agent(self):
        agent = astore.create("test", 1, POLICY, ["hk"], 20)
        agent["state"] = "stopped"
        astore.save(agent)
        job = rstore.create_agent_job(agent["agent_id"], "test", 1, ["hk"], 20, 2.0)
        runner.run_agent(job)
        saved = rstore.load_job(job["job_id"])
        self.assertEqual(saved["state"], "stopped")
        self.assertEqual(saved["agent_state"], "stopped")

    def test_resume_agents_requeues_when_children_done(self):
        job = rstore.create_agent_job("agt_20260101000000_abcd", "test", 1, ["hk"], 20, 2.0)
        job.update(state="waiting_children", child_jobs=["c1", "c2"])
        rstore.write_job(job)
        rstore.write_job({"job_id": "c1", "state": "done", "run_id": "r1"})
        rstore.write_job({"job_id": "c2", "state": "done", "run_id": "r2"})
        (self.runtime / "results" / "runs" / "r1").mkdir(parents=True)
        (self.runtime / "results" / "runs" / "r2").mkdir(parents=True)
        runner.resume_agents()
        self.assertEqual(rstore.load_job(job["job_id"])["state"], "queued")

    def test_recover_requeues_agent_and_fails_other_running(self):
        agent_job = rstore.create_agent_job("agt_20260101000000_abcd", "test", 1, ["hk"], 20, 2.0)
        agent_job["state"] = "running"
        rstore.write_job(agent_job)
        normal = {"job_id": "20260101000000_abcd_0_hk", "state": "running", "kind": None,
                  "research_id": "test", "version": 1, "market": "hk", "horizon": 20,
                  "loops": 1, "budget_usd": 1.0}
        rstore.write_job(normal)
        runner.recover()
        self.assertEqual(rstore.load_job(agent_job["job_id"])["state"], "queued")
        self.assertEqual(rstore.load_job(normal["job_id"])["state"], "failed")


if __name__ == "__main__":
    unittest.main()
