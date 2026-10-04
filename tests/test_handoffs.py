from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace, FrozenInstanceError
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text, inspect
from sqlalchemy.exc import IntegrityError

from app.application.handoffs import (CreateHandoff, AcceptDecision, CancelHandoff,
    create_handoff, accept_decision, cancel_handoff, read_orchestration)
from app.application.chatgpt_responses import ImportChatGptResponseCommand, import_chatgpt_response
from app.application.executions import evaluate_project_execution
from app.application.prompt_dispatches import CreatePromptDispatchCommand, create_prompt_dispatch
from app.application.prompt_deliveries import prepare_prompt_deliveries_for_send, acknowledge_prompt_delivery
from app.application.projects import ProjectCatalog
from app.application.roadmaps import RoadmapIssue
from app.config import Settings
from app.domain.handoff import HandoffStatus, DecisionEffect, OrchestrationConflict
from app.domain.execution import ExecutionEvidence, PullRequestEvidence, WorkflowRunEvidence
from app.domain.project import Project
from app.infrastructure.database import build_engine, build_session_factory, upgrade_database
from app.infrastructure.prompt_dispatches import SqlAlchemyUnitOfWork

PROJECT = Project('DevCockpit', 'jctrottier9-gif/DevCockpit', 1)
KEY = 'DC-040'


class Roadmap:
    key = KEY
    def read(self, project):
        return RoadmapIssue(project.repository_full_name, 1, f'''<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
{self.key} | WORK | READY | #1 | MAIN | Test work
<!-- /COCKPIT_PIPELINE_V1 -->''', '2026-10-01T12:00:00Z')


class Evidence:
    attempt = 1
    red = False
    head = 'abc123'
    def read(self, project, item):
        if not self.red:
            return ExecutionEvidence(default_branch='main')
        return ExecutionEvidence(default_branch='main', pull_requests=(PullRequestEvidence(
            number=42, title=f'{KEY} — Test', body='', branch='dc-040-test',
            head_sha=self.head, state='open', merged=False, mergeable=False,
            url='https://github.example/pr/42', updated_at='2026-10-01T12:00:00Z'),),
            workflow_runs=(WorkflowRunEvidence(run_id=123, name='CI', status='completed',
                conclusion='failure', attempt=self.attempt, head_sha=self.head,
                url='https://github.example/run/123', failed_jobs=('backend',)),))


@pytest.fixture
def env(tmp_path):
    settings = Settings(database_url=f'sqlite:///{tmp_path}/handoffs.db', execution_poll_seconds=0)
    upgrade_database(settings)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    uow = lambda: SqlAlchemyUnitOfWork(factory)
    result = {'settings': settings, 'engine': engine, 'uow': uow,
              'roadmap': Roadmap(), 'evidence': Evidence()}
    result['source'] = source(result)
    yield result
    engine.dispose()


def source(env, project='DevCockpit', key=KEY, role='DEV'):
    return create_prompt_dispatch(CreatePromptDispatchCommand(project, key, role, 'DEV source', str(uuid4())), uow_factory=env['uow'])


def create(env, command=None):
    command = command or CreateHandoff(uuid4(), env['source'].dispatch_id, 'Quelle approche ?', 'Contexte confirmé', 'JC')
    return create_handoff(PROJECT, KEY, command, roadmap_reader=env['roadmap'], uow_factory=env['uow'])


def response(env, dispatch_id):
    outbound = prepare_prompt_deliveries_for_send(uow_factory=env['uow'])
    delivery = next(d for d in outbound if d.dispatch_id == dispatch_id)
    cmd = ImportChatGptResponseCommand(uuid4(), delivery.delivery_id, delivery.session, 'Recommandation ARCH')
    import_chatgpt_response(cmd, uow_factory=env['uow'])
    return cmd


def accept(env, h, command=None):
    command = command or AcceptDecision(uuid4(), h.version, response(env, h.request_dispatch_id).response_id,
        'Conserver la frontière transactionnelle.', DecisionEffect.CONTINUE_IN_SCOPE, 'JC')
    return accept_decision(h.handoff_id, command, project_catalog=ProjectCatalog((PROJECT,)),
        roadmap_reader=env['roadmap'], evidence_reader=env['evidence'], uow_factory=env['uow'])


def view(env):
    return read_orchestration(PROJECT, KEY, roadmap_reader=env['roadmap'], evidence_reader=env['evidence'], uow_factory=env['uow'])


def test_parallel_dev_handoff_targets_selected_work_item_not_main(env):
    parallel_key = "DC-PAR"

    class ParallelRoadmap:
        def read(self, project):
            return RoadmapIssue(
                project.repository_full_name,
                1,
                """<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
DC-040 | WORK | READY | #1 | MAIN | Main work | - | -
DC-PAR | WORK | READY | #1 | PARALLEL | Parallel work | - | -
<!-- /COCKPIT_PIPELINE_V3 -->""",
                "2026-10-04T20:00:00Z",
            )

    roadmap = ParallelRoadmap()
    parallel_source = source(env, key=parallel_key)

    initial_view = read_orchestration(
        PROJECT,
        parallel_key,
        roadmap_reader=roadmap,
        evidence_reader=env["evidence"],
        uow_factory=env["uow"],
    )
    assert initial_view["execution_projection"]["work_item"]["key"] == parallel_key
    assert [item["agent_session"] for item in initial_view["dev_sources"]] == [
        "DevCockpit:DEV:DC-PAR"
    ]

    handoff = create_handoff(
        PROJECT,
        parallel_key,
        CreateHandoff(
            uuid4(),
            parallel_source.dispatch_id,
            "Quelle approche parallèle ?",
            "Contexte parallèle confirmé",
            "JC",
        ),
        roadmap_reader=roadmap,
        uow_factory=env["uow"],
    )
    imported = response(env, handoff.request_dispatch_id)
    resumed = accept_decision(
        handoff.handoff_id,
        AcceptDecision(
            uuid4(),
            handoff.version,
            imported.response_id,
            "Poursuivre uniquement le DEV parallèle.",
            DecisionEffect.CONTINUE_IN_SCOPE,
            "JC",
        ),
        project_catalog=ProjectCatalog((PROJECT,)),
        roadmap_reader=roadmap,
        evidence_reader=env["evidence"],
        uow_factory=env["uow"],
    )

    assert resumed.status == HandoffStatus.RESUME_PREPARED
    with env["uow"]() as uow:
        resume = uow.prompt_dispatches.get(resumed.resume_dispatch_id)
        assert resume.work_item_id == parallel_key
        assert resume.agent_session == "DevCockpit:DEV:DC-PAR"
        assert uow.handoffs.list_for_work_item(PROJECT.project_id, KEY) == []

    final_view = read_orchestration(
        PROJECT,
        parallel_key,
        roadmap_reader=roadmap,
        evidence_reader=env["evidence"],
        uow_factory=env["uow"],
    )
    assert final_view["work_item_id"] == parallel_key
    assert final_view["execution_projection"]["work_item"]["key"] == parallel_key


def test_full_loop_replay_multiple_responses_and_frozen_decision(env):
    cmd = CreateHandoff(uuid4(), env['source'].dispatch_id, 'Question', 'Contexte', 'JC')
    h = create(env, cmd)
    assert create(env, cmd) == h
    r1 = response(env, h.request_dispatch_id)
    r2 = response(env, h.request_dispatch_id)
    assert len(view(env)['handoffs'][0]['responses']) == 2
    with env['uow']() as u:
        assert u.handoffs.get(h.handoff_id).status == HandoffStatus.OPEN
        assert u.decisions.for_handoff(h.handoff_id) is None
    cmd2 = AcceptDecision(uuid4(), h.version, r1.response_id, 'Conclusion acceptée', DecisionEffect.CONTINUE_IN_SCOPE, 'JC')
    result = accept(env, h, cmd2)
    assert result.status == HandoffStatus.RESUME_PREPARED
    assert result.version == 3
    assert accept(env, h, cmd2) == result
    with env['uow']() as u:
        arch = u.prompt_dispatches.get(h.request_dispatch_id)
        resume = u.prompt_dispatches.get(result.resume_dispatch_id)
        decision = u.decisions.for_handoff(h.handoff_id)
        assert arch.agent_session == 'DevCockpit:ARCH:DC-040'
        assert resume.agent_session == env['source'].agent_session
        assert decision.source_response_id != r2.response_id
        assert decision.accepted_at.tzinfo is not None
        assert 'Conclusion acceptée' in resume.prompt_text
        with pytest.raises(FrozenInstanceError):
            decision.summary = 'changed'
    assert view(env)['handoffs'][0]['resume_dispatch']['dispatch_id'] == result.resume_dispatch_id
    with pytest.raises(OrchestrationConflict):
        accept(env, h, replace(cmd2, summary='different'))
    with pytest.raises(OrchestrationConflict):
        create(env, replace(cmd, question='different'))


@pytest.mark.parametrize('field', ['question','context','created_by'])
def test_blank_creation_rolls_back(env, field):
    cmd = CreateHandoff(uuid4(), env['source'].dispatch_id, 'Question', 'Context', 'JC')
    with pytest.raises(ValueError):
        create(env, replace(cmd, **{field:'  '}))
    with env['uow']() as u:
        assert u.handoffs.list_for_work_item(PROJECT.project_id, KEY) == []
        assert len(u.prompt_dispatches.list_for_work_item(PROJECT.project_id, KEY)) == 1


@pytest.mark.parametrize('project,key,role', [('Other', KEY, 'DEV'), ('DevCockpit','OTHER','DEV'), ('DevCockpit',KEY,'ARCH')])
def test_source_provenance_rejected(env, project, key, role):
    invalid = source(env, project, key, role)
    with pytest.raises(OrchestrationConflict):
        create(env, CreateHandoff(uuid4(), invalid.dispatch_id, 'Q','C','JC'))


def test_response_source_provenance_rejected(env):
    other = source(env)
    r = response(env, other.dispatch_id)
    with pytest.raises(OrchestrationConflict):
        create(env, CreateHandoff(uuid4(), env['source'].dispatch_id, 'Q','C','JC', r.response_id))


@pytest.mark.parametrize('effect', [DecisionEffect.HOLD_FOR_AUTHORIZATION, DecisionEffect.CONTINUE_IN_SCOPE])
def test_hold_or_changed_roadmap_retains_decision(env, effect):
    h = create(env)
    r = response(env, h.request_dispatch_id)
    if effect == DecisionEffect.CONTINUE_IN_SCOPE:
        env['roadmap'].key = 'DC-041'
    cmd = AcceptDecision(uuid4(), 1, r.response_id, 'Need authorization', effect, 'JC')
    held = accept(env, h, cmd)
    assert held.status == HandoffStatus.DECIDED
    assert held.resume_dispatch_id is None
    assert held.resume_held_reason
    assert view(env)['actions']['automatic_dev_inhibited']
    with env['uow']() as u:
        assert u.decisions.for_handoff(h.handoff_id)
    cancelled = cancel_handoff(h.handoff_id, CancelHandoff(uuid4(), held.version, 'JC','close'), uow_factory=env['uow'])
    assert cancelled.status == HandoffStatus.CANCELLED


def test_cancellation_late_response_and_reused_arch_session(env):
    h = create(env)
    outbound = prepare_prompt_deliveries_for_send(uow_factory=env['uow'])
    arch = next(d for d in outbound if d.dispatch_id == h.request_dispatch_id)
    cmd = CancelHandoff(uuid4(), h.version, 'JC', 'No longer needed')
    cancelled = cancel_handoff(h.handoff_id, cmd, uow_factory=env['uow'])
    assert cancel_handoff(h.handoff_id, cmd, uow_factory=env['uow']) == cancelled
    r = ImportChatGptResponseCommand(uuid4(), arch.delivery_id, arch.session, 'Late')
    import_chatgpt_response(r, uow_factory=env['uow'])
    with pytest.raises(OrchestrationConflict):
        accept(env, h, AcceptDecision(uuid4(), cancelled.version, r.response_id, 'No', DecisionEffect.CONTINUE_IN_SCOPE, 'JC'))
    second = create(env)
    with env['uow']() as u:
        assert u.prompt_dispatches.get(second.request_dispatch_id).agent_session == arch.session
        assert u.chatgpt_responses.get(r.response_id).text == 'Late'
    with pytest.raises(OrchestrationConflict):
        accept(env, second, AcceptDecision(uuid4(), second.version, r.response_id, 'Wrong handoff', DecisionEffect.CONTINUE_IN_SCOPE, 'JC'))


@pytest.mark.parametrize('project,key,role', [('Other',KEY,'ARCH'), ('DevCockpit','OTHER','ARCH'), ('DevCockpit',KEY,'DEV'), ('DevCockpit',KEY,'ARCH')])
def test_accept_wrong_dispatch_provenance(env, project, key, role):
    wrong = source(env, project, key, role)
    r = response(env, wrong.dispatch_id)
    h = create(env)
    with pytest.raises(OrchestrationConflict):
        accept(env, h, AcceptDecision(uuid4(), 1, r.response_id, 'Wrong source', DecisionEffect.CONTINUE_IN_SCOPE, 'JC'))


def test_inhibition_projection_and_covered_ci_cycle(env):
    env['evidence'].red = True
    def evaluate():
        return evaluate_project_execution(PROJECT, roadmap_reader=env['roadmap'], evidence_reader=env['evidence'], uow_factory=env['uow'])
    red = evaluate()
    assert red.dispatch
    h = create(env)
    suppressed = evaluate()
    assert suppressed.projection == red.projection
    assert suppressed.dispatch is None
    assert all(d.dispatch_id not in (red.dispatch.dispatch_id, env['source'].dispatch_id)
               for d in prepare_prompt_deliveries_for_send(uow_factory=env['uow']))
    resumed = accept(env, h)
    assert resumed.covered_github_evidence
    assert evaluate().dispatch is None
    env['evidence'].attempt = 2
    assert evaluate().dispatch
    env['evidence'].head = 'newhead'
    assert evaluate().dispatch


def test_initial_inhibited_and_delivered_prompt_limitation(env):
    before = prepare_prompt_deliveries_for_send(uow_factory=env['uow'])[0]
    acknowledge_prompt_delivery(before.delivery_id, uow_factory=env['uow'])
    create(env)
    result = evaluate_project_execution(PROJECT, roadmap_reader=env['roadmap'], evidence_reader=env['evidence'], uow_factory=env['uow'])
    assert result.dispatch is None
    with env['uow']() as u:
        assert u.prompt_deliveries.get(before.delivery_id).is_acknowledged
        assert u.prompt_dispatches.get(before.dispatch_id).status == 'CANCELLED'
    assert 'révoqué' in view(env)['transport_limitation']


def test_creation_and_acceptance_rollback_fault_injection(env, monkeypatch):
    from app.infrastructure.handoffs import SqlAlchemyHandoffRepository
    original_add = SqlAlchemyHandoffRepository.add
    def fail(*args):
        raise RuntimeError('injected failure')
    monkeypatch.setattr(SqlAlchemyHandoffRepository, 'add', fail)
    with pytest.raises(RuntimeError): create(env)
    with env['uow']() as u:
        assert len(u.prompt_dispatches.list_for_work_item(PROJECT.project_id, KEY)) == 1
    monkeypatch.setattr(SqlAlchemyHandoffRepository, 'add', original_add)
    h = create(env)
    r = response(env, h.request_dispatch_id)
    monkeypatch.setattr(SqlAlchemyHandoffRepository, 'save', fail)
    with pytest.raises(RuntimeError):
        accept(env, h, AcceptDecision(uuid4(), 1, r.response_id,'Conclusion',DecisionEffect.CONTINUE_IN_SCOPE,'JC'))
    with env['uow']() as u:
        assert u.handoffs.get(h.handoff_id) == h
        assert u.decisions.for_handoff(h.handoff_id) is None
        assert len(u.prompt_dispatches.list_for_work_item(PROJECT.project_id, KEY)) == 2


def parallel(*operations):
    barrier = Barrier(len(operations))
    def run(operation):
        barrier.wait()
        try: return operation()
        except OrchestrationConflict: return 'conflict'
    with ThreadPoolExecutor(len(operations)) as pool:
        return list(pool.map(run, operations))


def test_concurrent_creation_unique_and_idempotent(env):
    cmd = CreateHandoff(uuid4(), env['source'].dispatch_id, 'Q','C','JC')
    same = parallel(lambda:create(env,cmd),lambda:create(env,cmd))
    assert same[0] == same[1]
    cancel_handoff(same[0].handoff_id, CancelHandoff(uuid4(),1,'JC','restart'),uow_factory=env['uow'])
    results = parallel(lambda:create(env),lambda:create(env))
    assert results.count('conflict') == 1


@pytest.mark.parametrize('cancel', [False, True])
def test_concurrent_accept_or_cancel(env, cancel):
    h = create(env)
    r = response(env, h.request_dispatch_id)
    cmd = AcceptDecision(uuid4(),1,r.response_id,'Conclusion',DecisionEffect.CONTINUE_IN_SCOPE,'JC')
    other = (lambda:cancel_handoff(h.handoff_id,CancelHandoff(uuid4(),1,'JC','stop'),uow_factory=env['uow'])) if cancel else (lambda:accept(env,h,replace(cmd,acceptance_command_id=uuid4())))
    results = parallel(lambda:accept(env,h,cmd),other)
    assert results.count('conflict') == 1
    with env['uow']() as u:
        current = u.handoffs.get(h.handoff_id)
        assert current.status in (HandoffStatus.CANCELLED,HandoffStatus.RESUME_PREPARED)


def test_concurrent_poller_handoff_serializes(env):
    env['evidence'].red = True
    parallel(lambda:create(env), lambda:evaluate_project_execution(PROJECT,roadmap_reader=env['roadmap'],evidence_reader=env['evidence'],uow_factory=env['uow']))
    with env['uow']() as u:
        assert u.handoffs.active(PROJECT.project_id,KEY)
        assert all(d.role != 'DEV' for d in u.prompt_dispatches.list_prepared())


def test_stale_versions_invalid_transitions_and_unique_handoff(env):
    h = create(env)
    with pytest.raises(OrchestrationConflict): create(env)
    with pytest.raises(OrchestrationConflict): h.transition(HandoffStatus.DECIDED,99)
    with pytest.raises(OrchestrationConflict): h.transition(HandoffStatus.RESUME_PREPARED,1)
    with pytest.raises(OrchestrationConflict):
        cancel_handoff(h.handoff_id,CancelHandoff(uuid4(),99,'JC','reason'),uow_factory=env['uow'])
    resumed = accept(env,h)
    with pytest.raises(OrchestrationConflict):
        cancel_handoff(h.handoff_id,CancelHandoff(uuid4(),resumed.version,'JC','reason'),uow_factory=env['uow'])


def test_database_constraints_and_foreign_keys(env):
    h = create(env)
    engine = env['engine']
    for sql, params in [
        ('DELETE FROM prompt_dispatches WHERE dispatch_id=:id',{'id':str(h.request_dispatch_id)}),
        ('UPDATE handoffs SET source_dispatch_id=:id',{'id':str(uuid4())}),
        ("UPDATE handoffs SET question=' '",{}),
        ("UPDATE handoffs SET version=0",{}),
        ("UPDATE handoffs SET status='INVALID'",{}),
        ("UPDATE handoffs SET status='RESUME_PREPARED'",{}),
    ]:
        with pytest.raises(IntegrityError), engine.begin() as c:
            c.execute(text(sql),params)
    info = inspect(engine)
    assert {'uq_handoffs_active','ix_handoffs_project_work_status'} <= {i['name'] for i in info.get_indexes('handoffs')}
    resumed = accept(env,h)
    with pytest.raises(IntegrityError),engine.begin() as c:
        c.execute(text('DELETE FROM handoffs WHERE handoff_id=:id'),{'id':str(h.handoff_id)})
    with pytest.raises(IntegrityError),engine.begin() as c:
        c.execute(text("UPDATE decisions SET effect='INVALID'"))
    with env['uow']() as u:
        assert u.handoffs.get(h.handoff_id).created_at == h.created_at
        assert u.handoffs.get(h.handoff_id).resume_dispatch_id == resumed.resume_dispatch_id


def test_upgrade_from_0003_preserves_existing_history(tmp_path):
    settings = Settings(database_url=f'sqlite:///{tmp_path}/upgrade.db')
    upgrade_database(settings, '0003_imported_chatgpt_response')
    engine = build_engine(settings)
    sessions = build_session_factory(engine)
    uow = lambda: SqlAlchemyUnitOfWork(sessions)
    before = {'uow':uow}
    dispatch = source(before)
    delivery_id = uuid4()
    response_id = uuid4()
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO prompt_deliveries (
                delivery_id, dispatch_id, status, attempt_count,
                last_attempt_at, acknowledged_at, created_at, updated_at
            ) VALUES (
                :delivery_id, :dispatch_id, 'ACKNOWLEDGED', 1,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
        """), {
            'delivery_id': str(delivery_id),
            'dispatch_id': str(dispatch.dispatch_id),
        })
        connection.execute(text("""
            INSERT INTO imported_chatgpt_responses (
                response_id, delivery_id, text, imported_at
            ) VALUES (
                :response_id, :delivery_id, 'Recommandation ARCH', CURRENT_TIMESTAMP
            )
        """), {
            'response_id': str(response_id),
            'delivery_id': str(delivery_id),
        })
    with uow() as u:
        imported = u.chatgpt_responses.get(response_id)
    upgrade_database(settings)
    upgrade_database(settings)
    with uow() as u:
        assert u.chatgpt_responses.get(response_id).text == imported.text
        assert u.chatgpt_responses.get(response_id).imported_at == imported.imported_at
        assert u.prompt_dispatches.get(dispatch.dispatch_id).prompt_text == dispatch.prompt_text
        assert u.handoffs.list_for_work_item(PROJECT.project_id,KEY) == []
    with engine.connect() as c:
        assert c.execute(text('PRAGMA foreign_key_check')).all() == []
        assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == '0009_chatgpt_prompt_send'
    engine.dispose()


def test_api_full_flow_and_explicit_validation(env):
    from fastapi.testclient import TestClient
    from app.main import create_app
    app = create_app(env['settings'], project_catalog=ProjectCatalog((PROJECT,)),
        roadmap_reader=env['roadmap'],execution_reader=env['evidence'])
    with TestClient(app) as client:
        url = f'/api/projects/DevCockpit/work-items/{KEY}'
        body = {'creation_command_id':str(uuid4()),'source_dispatch_id':str(env['source'].dispatch_id),
                'question':'Question','context':'Context','created_by':'JC'}
        assert client.post('/api/projects/Unknown/work-items/DC-040/handoffs',json=body).status_code == 404
        result = client.post(url+'/handoffs',json=body)
        assert result.status_code == 200, result.text
        h = result.json()
        assert client.post(url+'/handoffs',json=body).json() == h
        assert client.post(url+'/handoffs',json={**body,'question':'Changed'}).status_code == 409
        from uuid import UUID
        r = response(env,UUID(h['request_dispatch_id']))
        read = client.get(url+'/orchestration')
        assert read.status_code == 200,read.text
        assert read.json()['handoffs'][0]['actions']['accept']
        command = {'acceptance_command_id':str(uuid4()),'expected_version':1,'source_response_id':str(r.response_id),
                   'summary':'Conclusion','effect':'CONTINUE_IN_SCOPE','accepted_by':'JC'}
        accept_url = f"/api/handoffs/{h['handoff_id']}/decisions"
        assert client.post(accept_url,json={**command,'effect':'AUTO_APPROVE'}).status_code == 422
        accepted = client.post(accept_url,json=command)
        assert accepted.status_code == 200,accepted.text
        assert accepted.json()['status'] == 'RESUME_PREPARED'
        assert client.post(accept_url,json=command).json() == accepted.json()
        assert client.get(url+'/orchestration').json()['handoffs'][0]['decision']['summary'] == 'Conclusion'


def test_database_active_uniqueness_and_scoped_work_items(env):
    from app.infrastructure.handoffs import HandoffRecord, values
    h = create(env)
    arch = source(env,role='ARCH')
    duplicate = replace(h,handoff_id=uuid4(),creation_command_id=uuid4(),request_dispatch_id=arch.dispatch_id)
    with pytest.raises(IntegrityError),env['uow']() as u:
        u._require_session().add(HandoffRecord(**values(duplicate)))
        u.commit()
    # Same schema permits another WorkItem; there is no global singleton.
    other = replace(duplicate,work_item_id='DC-041')
    with env['uow']() as u:
        u._require_session().add(HandoffRecord(**values(other)))
        u.commit()


def test_duplicate_decision_unique_and_restrict_response(env):
    from app.infrastructure.handoffs import DecisionRecord,values
    h = create(env)
    accept(env,h)
    with env['uow']() as u:
        decision = u.decisions.for_handoff(h.handoff_id)
    with pytest.raises(IntegrityError),env['uow']() as u:
        u._require_session().add(DecisionRecord(**values(replace(decision,decision_id=uuid4(),acceptance_command_id=uuid4()))))
        u.commit()
    with pytest.raises(IntegrityError),env['engine'].begin() as c:
        c.execute(text('DELETE FROM imported_chatgpt_responses WHERE response_id=:id'),{'id':str(decision.source_response_id)})


def test_acceptance_replay_across_restart_and_cancellation_rollback(env,monkeypatch):
    from app.infrastructure.handoffs import SqlAlchemyHandoffRepository
    h = create(env)
    r = response(env,h.request_dispatch_id)
    cmd = AcceptDecision(uuid4(),1,r.response_id,'Conclusion',DecisionEffect.HOLD_FOR_AUTHORIZATION,'JC')
    held = accept(env,h,cmd)
    new_engine = build_engine(env['settings'])
    env['uow'] = lambda:SqlAlchemyUnitOfWork(build_session_factory(new_engine))
    assert accept(env,h,cmd) == held
    def fail(*args): raise RuntimeError('injected cancellation failure')
    monkeypatch.setattr(SqlAlchemyHandoffRepository,'save',fail)
    with pytest.raises(RuntimeError):
        cancel_handoff(h.handoff_id,CancelHandoff(uuid4(),held.version,'JC','stop'),uow_factory=env['uow'])
    with env['uow']() as u:
        assert u.handoffs.get(h.handoff_id) == held
        assert u.prompt_dispatches.get(h.request_dispatch_id).status == 'PREPARED'
    new_engine.dispose()
