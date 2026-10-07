"""Run synthetic probes; human-reviewed labels are mandatory for reliability claims."""
import argparse
import json
import math
import tempfile
from collections import defaultdict
from contextlib import nullcontext
from pathlib import Path

import evaluation as rubric
import fact_verification
from rendering import inspect_html
from code_runner import run_python
from server import AppHandler, MODELS, connect, html_inspection, init_db, load_env_file, now_iso

ROOT = Path(__file__).resolve().parent


def wilson(success, total):
    if not total:
        return None
    z=1.96; p=success/total; d=1+z*z/total
    center=(p+z*z/(2*total))/d
    delta=z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/d
    return [round(center-delta,4),round(center+delta,4)]


def metrics(runs, labels):
    gold={x['id']:x for x in labels if x.get('humanReviewed') is True and x.get('reviewer')}
    grouped=defaultdict(list)
    repeats=defaultdict(list)
    repeat_scores=defaultdict(list)
    # Use first run only for human agreement, avoiding pseudoreplication.
    seen=set()
    fact_pairs=[]
    missed_extractions=0
    fact_case_outcomes={'caught':[], 'reviewFlagged':[], 'falseClearPass':[], 'abstained':[]}
    for run in runs:
        if 'report' not in run:
            continue
        for axis,item in run['report']['axes'].items():
            level=item.get('dominantLevel',item['score'])
            repeats[(run['id'],axis)].append((item['status'],level))
            repeat_scores[(run['id'],axis)].append(item['score'])
        if run['id'] in seen or run['split']!='holdout' or run['id'] not in gold:
            continue
        if not run.get('caseHash') or gold[run['id']].get('caseHash') != run['caseHash']:
            continue
        seen.add(run['id'])
        missing=gold[run['id']].get('missingClaims',[])
        missing_relations=[]
        if isinstance(missing,list):
            for entry in missing:
                if isinstance(entry,str):
                    entry={'text':entry}
                if not isinstance(entry,dict) or not isinstance(entry.get('text'),str) or not entry['text'].strip():
                    continue
                missed_extractions+=1
                if entry.get('relation') in {'SUPPORTED','CONTRADICTED','UNVERIFIED','NOT_APPLICABLE'}:
                    missing_relations.append(entry['relation'])
                    fact_pairs.append((entry['relation'],None))
        observed_claims={item['id']:item for item in (run['report'].get('factVerification') or {}).get('claims',[])}
        human_contradiction='CONTRADICTED' in missing_relations
        for claim_id,expected in gold[run['id']].get('claims',{}).items():
            if not isinstance(expected,dict) or expected.get('relation') not in {
                    'SUPPORTED','CONTRADICTED','UNVERIFIED','NOT_APPLICABLE'}:
                continue
            observed=observed_claims.get(claim_id)
            fact_pairs.append((expected['relation'],observed.get('relation') if observed else None))
            human_contradiction |= expected['relation']=='CONTRADICTED'
        if human_contradiction:
            truth=run['report']['axes']['truthfulness']
            score=truth['score']
            outcome=('abstained' if score is None else 'caught' if score<3 else
                     'reviewFlagged' if run['report'].get('needsReview') else 'falseClearPass')
            fact_case_outcomes[outcome].append(run['id'])
        for axis,expected in gold[run['id']].get('axes',{}).items():
            if axis in rubric.LABELS and type(expected) is int and 0<=expected<=3:
                item=run['report']['axes'][axis]
                grouped[(run['category'],axis)].append((expected,item.get('dominantLevel',item['score'])))
    groups=[]
    for category in rubric.CATEGORIES:
        for axis in rubric.LABELS:
            pairs=grouped[(category,axis)]; n=len(pairs)
            exact=sum(a==b for a,b in pairs)
            available=sum(b is not None for a,b in pairs)
            major=sum(b is not None and abs(a-b)>=2 for a,b in pairs)
            false_positive=sum(a==3 and b is not None and b<3 for a,b in pairs)
            missed=sum(a<3 and b==3 for a,b in pairs)
            groups.append({'category':category,'axis':axis,'n':n,'agreement':exact/n if n else None,
                'agreement95CI':wilson(exact,n),'coverage':available/n if n else None,
                'majorErrors':major,'falsePositives':false_positive,'missedProblems':missed,
                'pass':n>=30 and exact/n>=.9 and major/n<=.05 and available/n>=.9})
    repeat_pairs=[values for values in repeats.values() if len(values)>=3]
    stable=sum(all(v==values[0] for v in values) for values in repeat_pairs)
    repeat_agreement=stable/len(repeat_pairs) if repeat_pairs else None
    score_ranges=[round(max(values)-min(values),4) for values in repeat_scores.values()
                  if len(values)>=3 and all(v is not None for v in values)]
    fact_total=len(fact_pairs)
    contradiction_total=sum(gold_relation=='CONTRADICTED' for gold_relation,_ in fact_pairs)
    contradiction_found=sum(gold_relation=='CONTRADICTED' and predicted=='CONTRADICTED'
                            for gold_relation,predicted in fact_pairs)
    false_contradictions=sum(gold_relation!='CONTRADICTED' and predicted=='CONTRADICTED'
                             for gold_relation,predicted in fact_pairs)
    return {'groups':groups,'humanLabeledHoldoutCases':len(seen),
            'factClaims':{'n':fact_total,
                          'humanMissingClaims':missed_extractions,
                          'relationAgreement':sum(a==b for a,b in fact_pairs)/fact_total if fact_total else None,
                          'contradictionRecall':contradiction_found/contradiction_total if contradiction_total else None,
                          'missedContradictions':contradiction_total-contradiction_found,
                          'falseContradictions':false_contradictions,
                          'unverifiedCount':sum(predicted=='UNVERIFIED' for _,predicted in fact_pairs),
                          'casesWithHumanContradiction':sum(len(ids) for ids in fact_case_outcomes.values()),
                          'falseClearPasses':len(fact_case_outcomes['falseClearPass']),
                          'falseClearPassRate':(len(fact_case_outcomes['falseClearPass']) /
                                                sum(len(ids) for ids in fact_case_outcomes.values())
                                                if any(fact_case_outcomes.values()) else None),
                          'caseOutcomes':fact_case_outcomes},
            'repeatGroups':len(repeat_pairs),'repeatAgreement':repeat_agreement,
            'repeatScoreRangeMean':sum(score_ranges)/len(score_ranges) if score_ranges else None,
            'repeatScoreRangeMax':max(score_ranges) if score_ranges else None,
            'reliability':'not_validated',
            'automaticThresholdsMet':all(g['pass'] for g in groups) and repeat_agreement is not None and repeat_agreement>=.95,
            'note':'중요 오류/안전 사례의 충분성과 독립 라벨을 사람이 검토한 후 사용 범위를 승인해야 합니다. 소표본/AI 작성 예제는 신뢰성 입증이 아닙니다.'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true',help='실제 JEV 호출 (등록한 자료가 API로 전송됨)')
    parser.add_argument('--split',choices=['calibration','holdout','all'],default='calibration')
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--model',default='jev-1.13.0')
    parser.add_argument('--cases',type=Path,default=ROOT/'validation/cases.json')
    parser.add_argument('--labels',type=Path)
    parser.add_argument('--report-from',type=Path,help='저장한 실행 결과에 사람 라벨을 대조; API 호출 없음')
    parser.add_argument('--output',type=Path,default=ROOT/'validation/latest-run.json')
    args=parser.parse_args()
    if args.report_from:
        previous=json.loads(args.report_from.read_text(encoding='utf-8'))
        labels=json.loads(args.labels.read_text(encoding='utf-8')) if args.labels else []
        report={'sourceRun':str(args.report_from),'rubricVersion':previous['rubricVersion'],
                'createdAt':now_iso(),'metrics':metrics(previous['runs'],labels)}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Saved '+str(args.output)); return
    if not 1<=args.repeats<=10: parser.error('repeats must be 1..10')
    if args.live and args.model.endswith('latest'): parser.error('반복 검증에는 고정 버전을 사용하세요.')
    load_env_file()
    cases=json.loads(args.cases.read_text(encoding='utf-8'))
    labels=json.loads(args.labels.read_text(encoding='utf-8')) if args.labels else []
    selected=[c for c in cases if args.split=='all' or c['split']==args.split]
    runs=[]
    output={'rubricVersion':rubric.VERSION,'fixtureHash':rubric.digest(cases),'model':args.model,
            'live':args.live,'createdAt':now_iso(),'fixtureProvenance':'AI-authored synthetic probes, not independent human labels','runs':runs}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    template=[]
    for c in cases:
        item={'id':c['id'],'caseHash':rubric.digest(c),'reviewer':'','humanReviewed':False,
              'axes':{a:None for a in rubric.LABELS}}
        if c['category']!='코딩':
            claim_text=c['response']
            if c.get('outputFormat')=='html':
                rendered=inspect_html(c['response'],ROOT/'data/artifacts')
                claim_text=(rendered.get('observations') or {}).get('visibleText') or claim_text
            claims,_=fact_verification.candidates(claim_text)
            item['claims']={cid:{'text':claim,'relation':None,'importance':None} for cid,claim in claims.items()}
            item['missingClaims']=[]
        template.append(item)
    template_path=args.output.parent/f'{args.cases.stem}.human-labels.template.json'
    if not template_path.exists(): template_path.write_text(json.dumps(template,ensure_ascii=False,indent=2),encoding='utf-8')
    for c in selected:
        spec=rubric.normalize_spec({'requirements':c['requirements'],'confirmed':True,'outputFormat':c.get('outputFormat','text'),
            'expectedAnswer':c.get('expectedAnswer',''),'checks':c.get('checks',[]),'codeTests':c.get('codeTests',{})},c['prompt'])
        case={'category':c['category'],'prompt':c['prompt'],'attachment_text':c.get('attachment',''),
              'conversation_history':'','evaluation_spec_json':json.dumps(spec,ensure_ascii=False)}
        context=tempfile.TemporaryDirectory() if args.live else nullcontext(None)
        with context as temporary:
            if args.live:
                handler=object.__new__(AppHandler)
                handler.db_path=Path(temporary)/'validation.db'
                handler.evaluator_model=args.model
                init_db(handler.db_path)
                created=handler.create_case({'title':c['id'],'category':c['category'],'prompt':c['prompt'],
                    'attachmentText':c.get('attachment',''),'responses':{MODELS[0]:c['response']},
                    'evaluationSpec':spec})
                response_id=created['responses'][0]['id']
            else:
                inspection=None
                if spec['outputFormat']=='html':
                    inspection=html_inspection(c['response']); inspection.update(inspect_html(c['response'],ROOT/'data/artifacts'))
                execution=run_python(rubric.python_source(c['response']),spec['codeTests']) if spec['codeTests'] else None
                state,questions=rubric.prepare(case,{'content':c['response']},inspection,execution)
            for repeat in range(args.repeats if args.live else 1):
                run={'id':c['id'],'category':c['category'],'split':c['split'],'repeat':repeat+1,
                     'caseHash':rubric.digest(c)}
                if args.live:
                    try:
                        evaluated=handler.evaluate(response_id,force=repeat>0)
                        run['report']=evaluated['report']
                        run['inputHash']=evaluated['report']['inputHash']
                        run['checks']=evaluated['report']['checks']
                        with connect(handler.db_path) as db:
                            saved=db.execute('SELECT raw_json FROM evaluations WHERE id = ?',
                                             (evaluated['id'],)).fetchone()
                        run['raw']=json.loads(saved['raw_json'])['quality']
                    except (RuntimeError,ValueError) as error:
                        run['error']=str(error)
                else:
                    run['inputHash']=rubric.digest(state)
                    run['checks']=state['checks']
                runs.append(run)
                output['metrics']=metrics(runs,labels)
                args.output.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps({'id':c['id'],'repeat':repeat+1,'error':run.get('error'),
                    'axes':{k:v['score'] for k,v in run.get('report',{}).get('axes',{}).items()}},ensure_ascii=True),flush=True)
                if 'error' in run:
                    raise SystemExit(1)
    print('Saved '+str(args.output),flush=True)


if __name__=='__main__': main()
