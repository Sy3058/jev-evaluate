"""Run synthetic probes; human-reviewed labels are mandatory for reliability claims."""
import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

import evaluation as rubric
from rendering import inspect_html
from code_runner import run_python
from server import call_jev, html_inspection, load_env_file, now_iso

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
    # Use first run only for human agreement, avoiding pseudoreplication.
    seen=set()
    for run in runs:
        if 'report' not in run:
            continue
        for axis,item in run['report']['axes'].items():
            repeats[(run['id'],axis)].append((item['status'],item['score']))
        if run['id'] in seen or run['split']!='holdout' or run['id'] not in gold:
            continue
        if not run.get('caseHash') or gold[run['id']].get('caseHash') != run['caseHash']:
            continue
        seen.add(run['id'])
        for axis,expected in gold[run['id']].get('axes',{}).items():
            if axis in rubric.LABELS and type(expected) is int and 0<=expected<=3:
                grouped[(run['category'],axis)].append((expected,run['report']['axes'][axis]['score']))
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
    return {'groups':groups,'humanLabeledHoldoutCases':len(seen),
            'repeatGroups':len(repeat_pairs),'repeatAgreement':repeat_agreement,
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
    template=[{'id':c['id'],'caseHash':rubric.digest(c),'reviewer':'','humanReviewed':False,'axes':{a:None for a in rubric.LABELS}} for c in cases]
    template_path=args.output.parent/f'{args.cases.stem}.human-labels.template.json'
    if not template_path.exists(): template_path.write_text(json.dumps(template,ensure_ascii=False,indent=2),encoding='utf-8')
    for c in selected:
        spec=rubric.normalize_spec({'requirements':c['requirements'],'confirmed':True,'outputFormat':c.get('outputFormat','text'),
            'expectedAnswer':c.get('expectedAnswer',''),'checks':c.get('checks',[]),'codeTests':c.get('codeTests',{})},c['prompt'])
        case={'category':c['category'],'prompt':c['prompt'],'attachment_text':c.get('attachment',''),
              'conversation_history':'','evaluation_spec_json':json.dumps(spec,ensure_ascii=False)}
        inspection=None
        if spec['outputFormat']=='html':
            inspection=html_inspection(c['response']); inspection.update(inspect_html(c['response'],ROOT/'data/artifacts'))
        execution=run_python(rubric.python_source(c['response']),spec['codeTests']) if spec['codeTests'] else None
        state,questions=rubric.prepare(case,{'content':c['response']},inspection,execution)
        for repeat in range(args.repeats if args.live else 1):
            run={'id':c['id'],'category':c['category'],'split':c['split'],'repeat':repeat+1,
                 'inputHash':rubric.digest(state),'caseHash':rubric.digest(c),'checks':state['checks']}
            if args.live:
                try:
                    raw=call_jev(state,args.model,questions)
                    run['report']=rubric.parse_result(raw,state,questions)
                    run['raw']=raw
                except (RuntimeError,ValueError) as error:
                    run['error']=str(error)
            runs.append(run)
            output['metrics']=metrics(runs,labels)
            args.output.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'id':c['id'],'repeat':repeat+1,'error':run.get('error'),
                'axes':{k:v['score'] for k,v in run.get('report',{}).get('axes',{}).items()}},ensure_ascii=True),flush=True)
            if 'error' in run:
                raise SystemExit(1)
    print('Saved '+str(args.output),flush=True)


if __name__=='__main__': main()
