#!/usr/bin/env python3
"""Re-optimize into an isolated directory and compare deterministic artifacts."""
import hashlib,json,shutil,subprocess,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
    names=['planning_result.json','planning_result.geojson','waypoints.csv','scan_visits.csv','candidate_comparison.json','feasibility_bounds.json','planner_config.json','return_selections.json','redundancy_audit.json','candidates/coverage_priority.json','candidates/more_reserve.json','candidates/coverage_priority_return_selections.json','candidates/more_reserve_return_selections.json','candidates/coverage_priority_redundancy_audit.json','candidates/more_reserve_redundancy_audit.json','return_catalog.json','return_catalog.schema.json','topic_mapping.json','planning_preview.html']
    with tempfile.TemporaryDirectory(prefix='planning_v2_reproduction_') as temp:
        out=Path(temp);shutil.copy2(ROOT/'planner_config.json',out/'planner_config.json')
        commands=[
            [sys.executable,str(ROOT/'planner.py'),'--input',str(ROOT/'inputs/02_规划输入_v2.json'),'--exclusions',str(ROOT/'exclusions_draft.json'),'--output-dir',str(out)],
            [sys.executable,str(ROOT/'build_return_catalog.py'),'--input',str(ROOT/'inputs/02_规划输入_v2.json'),'--plan',str(out/'planning_result.json'),'--exclusions',str(ROOT/'exclusions_draft.json'),'--selections',str(out/'return_selections.json'),'--output-dir',str(out)],
            [sys.executable,str(ROOT/'build_preview.py'),'--input',str(out/'planning_result.json'),'--output',str(out/'planning_preview.html')]
        ]
        for i,cmd in enumerate(commands,1):
            print('Reproduction stage %d/%d'%(i,len(commands)),flush=True)
            with (out/('stage%d.log'%i)).open('w',encoding='utf-8') as log:
                run=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT)
            if run.returncode:
                raise RuntimeError((out/('stage%d.log'%i)).read_text(encoding='utf-8'))
        checks=[]
        for name in names:
            a,b=sha(ROOT/name),sha(out/name);checks.append(dict(file=name,original_sha256=a,reproduced_sha256=b,byte_identical=a==b))
        report=dict(passed=all(c['byte_identical'] for c in checks),method='Actual full re-optimization in an isolated temporary directory, then regenerate all-recipient return catalog, topic mappings, schema and self-contained preview. Same immutable merged input, exclusions and timestamp snapshot; not reuse-candidates.',source_inputs_sha256=sha(ROOT/'inputs/02_规划输入_v2.json'),planning_result_sha256=sha(ROOT/'planning_result.json'),checks=checks)
        (ROOT/'reproducibility_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print('Byte-identical %d/%d'%(sum(c['byte_identical'] for c in checks),len(checks)))
        if not report['passed']:raise SystemExit(1)
if __name__=='__main__':main()
