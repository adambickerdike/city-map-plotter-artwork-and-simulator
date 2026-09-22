#!/usr/bin/env python3
"""Rebuild the final drawing from the source snapshot bundled with the package."""
import argparse,json,os,shutil,subprocess,sys,tempfile
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parent
PACKAGE=ROOT.parent
ID='aveling-porter-5499-colour'

def run(*args):
    env=dict(os.environ);env.pop('PYTHONPATH',None);env['PYTHONDONTWRITEBYTECODE']='1'
    subprocess.run([sys.executable,*map(str,args)],cwd=ROOT,env=env,check=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--skip-pdf',action='store_true',help='Rebuild and verify plot files without the conventional-print PDF')
    args=parser.parse_args();out=args.output_dir.resolve()
    if out==PACKAGE:
        parser.error('Choose a separate rebuild directory to preserve the released files.')
    evidence=out/'evidence';evidence.mkdir(parents=True,exist_ok=True)
    facts=json.loads((PACKAGE/'evidence/revision-14-sources.json').read_text())
    names={'revision-14-master.svg','revision-14-master.plot.json','revision-14-sources.json','revision-14-reconstruction.json','revision-14-verification.json','revision-14-plotting-verification.json','Rolling_186_Autumn_2021.pdf','reference-landmarks.json'}
    names.update(s[k] for s in facts['sources'] for k in ['file','working_file','video_file'] if s.get(k))
    for name in names:shutil.copy2(PACKAGE/'evidence'/name,evidence/name)
    run('tools/engineering_source_plates/build_aveling_5499_colour.py','--output-dir',out)
    svg=out/'artwork'/f'{ID}.svg'
    profile=ROOT/'plotter-profiles/axidraw-class-simulation-v1.json'
    run('tools/plotjob.py','compile',svg,'--profile',profile,'--order','optimised',
        '--out',out/'plot/aveling-porter-5499-colour.plotjob.json')
    run('tools/engineering_source_plates/aveling_5499_colour_v1/verify.py',out)
    run('tools/build_plotsim_viewer.py',svg,'--out',out/'plot/aveling-porter-5499-colour-viewer.html',
        '--machine-profile',profile,'--strict-svg')
    if not args.skip_pdf:
        ns='http://www.w3.org/2000/svg';ET.register_namespace('',ns)
        tree=ET.parse(svg)
        tree.getroot().insert(0,ET.Element('{'+ns+'}rect',{'width':'420','height':'297','fill':'#ffffff'}))
        with tempfile.TemporaryDirectory(prefix='aveling-pdf-') as tmp:
            blue=Path(tmp)/'print-blue.svg';tree.write(blue,encoding='utf-8',xml_declaration=True)
            subprocess.run(['inkscape',str(blue),'--export-type=pdf',
                f'--export-filename={out/"artwork"/(ID+"-preview.pdf")}'],check=True)
    print(f'Rebuilt and verified: {out}')

if __name__=='__main__':main()
