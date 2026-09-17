"""Render only with the task-local official portable LibreOffice, never desktop Office."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile

qa=Path(__file__).resolve().parent
bundle=Path(r"C:\Users\94863\.cache\codex-runtimes\codex-primary-runtime")
skill=bundle/"plugins/openai-primary-runtime/plugins/documents/skills/documents/render_docx.py"
soffice=Path(tempfile.gettempdir())/"adf-docx-render-tools/LibreOfficeExtracted/App/libreoffice/program/soffice.exe"
if not soffice.is_file():
    raise SystemExit("Task-local portable LibreOffice is missing; desktop fallback is prohibited")
os.environ["PATH"]=str(bundle/"dependencies/native/poppler/Library/bin")+os.pathsep+str(soffice.parent)+os.pathsep+os.environ.get("PATH","")
spec=importlib.util.spec_from_file_location("render_docx",skill)
renderer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)
renderer._resolve_soffice=lambda:str(soffice)
sys.argv=[str(skill),str(qa.parent/"数据流转与存储设计.docx"),"--output_dir",str(qa/"render"),"--emit_pdf","--dpi","140","--verbose"]
renderer.main()

