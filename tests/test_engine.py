import asyncio

from switchyard.engine import Done, Engine, Rewind, StepDef, StepFatal, StepRetry, WorkflowDef


def wf(*steps, name="wf"):
    return WorkflowDef(name, 1, list(steps))


async def drain(engine, clock, rounds=20, step=5.0):
    for _ in range(rounds):
        if not await engine.tick():
            clock.advance(step)


async def test_dependencies_run_in_order_and_see_outputs(settings, clock):
    order = []

    def h(name):
        async def handler(ctx):
            order.append(name)
            seen = {d: ctx.output_of(d)["n"] for d in ctx.engine.workflows["wf"].step(name).depends_on}
            return Done({"n": name, "seen": seen})

        return handler

    flow = wf(
        StepDef("a", h("a")),
        StepDef("b", h("b"), ("a",)),
        StepDef("c", h("c"), ("a",)),
        StepDef("d", h("d"), ("b", "c")),
    )
    engine = Engine(settings, [flow], clock=clock)
    run_id, _ = engine.start_run("wf", {})
    await drain(engine, clock)
    assert order == ["a", "b", "c", "d"]
    assert engine.step_output(run_id, "d")["seen"] == {"b": "b", "c": "c"}
    assert engine.get_run(run_id)["status"] == "succeeded"


async def test_business_dedup_returns_the_existing_run(settings, clock):
    async def ok(ctx):
        return Done()

    flow = WorkflowDef("wf", 1, [StepDef("a", ok)], dedup=lambda i: f"order:{i['id']}")
    engine = Engine(settings, [flow], clock=clock)
    first, created = engine.start_run("wf", {"id": 7})
    second, created_again = engine.start_run("wf", {"id": 7})
    assert created and not created_again and first == second


async def test_retries_back_off_then_fail_and_report(settings, clock):
    async def flaky(ctx):
        raise StepRetry("downstream unavailable")

    engine = Engine(settings, [wf(StepDef("a", flaky, max_attempts=3))], clock=clock)
    run_id, _ = engine.start_run("wf", {})
    await drain(engine, clock, rounds=30)
    step = engine.steps(run_id)[0]
    assert step["status"] == "failed" and step["attempts"] == 3
    assert [x["kind"] for x in engine.reporter.list("open")] == ["step_error"]
    assert engine.get_run(run_id)["status"] == "failed"


async def test_failure_blocks_dependents_until_operator_retry(settings, clock):
    broken = {"yes": True}

    async def first(ctx):
        if broken["yes"]:
            raise StepFatal("credentials revoked", kind="tool_permanent")
        return Done({"ok": True})

    async def second(ctx):
        return Done({"after": ctx.output_of("a")["ok"]})

    async def independent(ctx):
        return Done()

    engine = Engine(
        settings, [wf(StepDef("a", first), StepDef("b", second, ("a",)), StepDef("x", independent))], clock=clock
    )
    run_id, _ = engine.start_run("wf", {})
    await drain(engine, clock)
    status = {s["name"]: s["status"] for s in engine.steps(run_id)}
    assert status == {"a": "failed", "b": "blocked", "x": "succeeded"}
    broken["yes"] = False
    engine.retry_step(run_id, "a", "lena.brandt")
    await drain(engine, clock)
    assert engine.get_run(run_id)["status"] == "succeeded"
    assert not engine.reporter.list("open")


async def test_rewinds_are_bounded(settings, clock):
    async def draft(ctx):
        return Done({"notes": len(ctx.notes)})

    async def review(ctx):
        return Rewind("draft", "still wrong")

    engine = Engine(settings, [wf(StepDef("draft", draft), StepDef("review", review, ("draft",)))], clock=clock)
    run_id, _ = engine.start_run("wf", {})
    await drain(engine, clock)
    status = {s["name"]: s for s in engine.steps(run_id)}
    assert status["review"]["status"] == "failed" and status["review"]["revisions"] == settings.max_review_revisions
    assert any(x["kind"] == "review_rejected" for x in engine.reporter.list("open"))


async def test_crashed_worker_lease_is_recovered(settings, clock):
    calls = []

    async def step(ctx):
        calls.append(ctx.engine.worker_id)
        return Done()

    flow = wf(StepDef("a", step))
    dead = Engine(settings, [flow], clock=clock, worker_id="dead")
    run_id, _ = dead.start_run("wf", {})
    assert dead._claim_next() is not None  # claimed, then the process "dies"
    alive = Engine(settings, [flow], clock=clock, worker_id="alive")
    assert await alive.tick() == 0  # lease still valid: nobody else may run it
    clock.advance(settings.lease_seconds + 1)
    await alive.tick()
    assert calls == ["alive"]
    assert alive.get_run(run_id)["status"] == "succeeded"
    assert any(e["event"] == "step.lease_expired" for e in alive.audit.entries(run_id))


async def test_zombie_worker_cannot_act_or_commit_after_losing_its_lease(settings, clock):
    release = asyncio.Event()

    async def step(ctx):
        if ctx.engine.worker_id == "zombie":
            await release.wait()  # stalls (GC pause, network partition…)
        ctx.act("chat.post", {"channel": "#t", "text": f"from {ctx.engine.worker_id}"}, slot=ctx.engine.worker_id)
        return Done({"by": ctx.engine.worker_id})

    flow = wf(StepDef("a", step))
    zombie = Engine(settings, [flow], clock=clock, worker_id="zombie")
    run_id, _ = zombie.start_run("wf", {})
    stalled = asyncio.create_task(zombie._run_step(*zombie._claim_next()))
    await asyncio.sleep(0)
    clock.advance(settings.lease_seconds + 1)
    healthy = Engine(settings, [flow], clock=clock, worker_id="healthy")
    await healthy.tick()
    release.set()
    await stalled
    assert [m["text"] for m in healthy.reads.chat_history("#t")] == ["from healthy"]
    assert healthy.step_output(run_id, "a") == {"by": "healthy"}
    assert any(e["event"] == "step.lease_lost" for e in healthy.audit.entries(run_id))
