"""Finite real-model Oracle trials with optional zero control and between-run memory."""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from importlib.resources import files
from fairy.events.artifacts import write_json,load_json
from fairy.events.provenance import source_identity
from evaluate.oracle.qualification import qualify,select


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True)
    parser.add_argument('--model',default=os.getenv('FAIRY_LLM_MODEL'))
    parser.add_argument('--base-url',default=os.getenv('FAIRY_LLM_BASE_URL'))
    parser.add_argument('--policy',help='Existing source-compatible trained checkpoint; otherwise train a new one')
    parser.add_argument('--attempts',type=int,default=2)
    parser.add_argument('--timesteps',type=int,default=2048)
    parser.add_argument('--max-steps',type=int,default=800)
    parser.add_argument('--zero-control',action='store_true')
    parser.add_argument('--llm-memory',action='store_true')
    args=parser.parse_args(argv)
    if not args.model or args.attempts<1:raise ValueError('Specify a model and positive attempt budget')
    if not (os.getenv('FAIRY_LLM_API_KEY') or os.getenv('OPENAI_API_KEY')):raise ValueError('Set model credentials in the environment')
    out=Path(args.out).resolve();out.mkdir(parents=True,exist_ok=False)
    source=source_identity();write_json(out/'source_manifest.json',source)
    with zipfile.ZipFile(out/'source_snapshot.zip','x',zipfile.ZIP_DEFLATED) as archive:
        for name in source['files']:
            package,relative=name.split('/',1)
            archive.writestr(name,files(package).joinpath(relative).read_bytes())
    common=['--model',args.model]
    if args.base_url:common+=['--base-url',args.base_url]
    def command(label,arguments):
        with (out/(label+'.log')).open('x') as stream:
            process=subprocess.run([sys.executable,'-m','interface.cli.oracle',*arguments],stdout=stream,stderr=subprocess.STDOUT)
        if source_identity()['fingerprint']!=source['fingerprint']:raise ValueError('Source changed during experiment')
        return process.returncode
    policy=Path(args.policy).resolve() if args.policy else out/'policy.zip'
    if not args.policy:
        if command('training',['train','--out',str(policy),'--timesteps',str(args.timesteps)])!=0:raise ValueError('Training failed; inspect local log')
        if command('numeric_evaluation',['evaluate','--policy',str(policy),'--out',str(out/'numeric_evaluation')])!=0:raise ValueError('Numerical evaluation failed')
    memory=out/'experiences.jsonl';reports={};attempts=[]
    conditions=([('zero_control','zero')] if args.zero_control else [])+[(f'ppo_{i:03d}','ppo') for i in range(args.attempts)]
    for label,numeric in conditions:
        destination=out/label
        argv=['generate','--numeric',numeric,'--out',str(destination),'--max-steps',str(args.max_steps),*common]
        if numeric=='ppo':argv+=['--policy',str(policy)]
        # Zero is an empty-memory control; later learning trials freeze the updated snapshot.
        if numeric=='ppo' and memory.exists():argv+=['--memory',str(memory)]
        code=command(label,argv)
        record_path=destination/'run.json'
        attempts.append({'name':label,'exit_code':code,'artifact':str(record_path) if record_path.exists() else None})
        if record_path.exists():
            report=qualify(load_json(record_path));reports[label]=report
            write_json(out/(label+'_independent_qualification.json'),report)
            if numeric=='ppo':
                induction=['induce',str(record_path),'--memory',str(memory)]
                if args.llm_memory:induction+=['--llm-condense','--out',str(out/(label+'_induction.json')),*common]
                status=command(label+'_memory',induction)
                attempts[-1]['induction_exit_code']=status
                if status!=0:break  # preserve failure; never silently replace failed model induction
    selection=select(reports)
    write_json(out/'summary.json',{'attempts':attempts,'qualification':reports,'selection':selection})
    for key,folder in [('selected','selected_oracle'),('selected_trained_stack','selected_trained_oracle')]:
        name=selection[key]
        if not name:continue
        record=load_json(out/name/'run.json');destination=out/folder;destination.mkdir()
        shutil.copyfile(out/name/'run.json',destination/'run.json')
        write_json(destination/'qualification.json',reports[name])
        write_json(destination/'knowledge_snapshot.json',record['agent']['autogen']['knowledge'])
        write_json(destination/'memory_snapshot.json',record['agent']['autogen']['memory_snapshot'])
        write_json(destination/'workflow.json',{'schema':1,'kind':'actual physical requests; canonical replay uses run.json',
            'requests':[e for e in record['state']['events'] if e['kind']=='operation_requested']})
    print(json.dumps(selection,ensure_ascii=False,indent=2))
    return 0 if selection['selected'] else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except KeyboardInterrupt:raise SystemExit(130)
    except Exception as exc:
        print('Oracle experiment failed: '+type(exc).__name__,file=sys.stderr)
        raise SystemExit(2)
