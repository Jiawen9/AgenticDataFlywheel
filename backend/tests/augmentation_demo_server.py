"""Serve the built UI with disposable augmentation jobs and a simulated model.

Run: python -m backend.tests.augmentation_demo_server --port 8792
All knowledge-base files, inputs, jobs and exports live in a temporary directory.
"""
from __future__ import annotations

import argparse
import time
import tempfile
from pathlib import Path

import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.task_generation.constants import PROJECT_ROOT
from backend.task_generation.jobs import TaskGenerationJobManager
from backend.task_generation.router import configure_job_manager, router
from backend.task_generation.service import run_augmentation_classification, run_augmentation_generation
from backend.tests.test_scene_tree_editing import SimulatedModel


class DemoModel(SimulatedModel):
    def complete(self, prompt, **kwargs):
        time.sleep(0.5)
        return super().complete(prompt, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8792)
    args = parser.parse_args()
    dist = PROJECT_ROOT / 'frontend' / 'dist'
    with tempfile.TemporaryDirectory(prefix='augmentation-browser-') as temporary:
        base = Path(temporary)
        kb = base / 'kb'
        kb.mkdir()
        branches = [
            {'scene': '出行导航', 'capability': '路线规划', 'sub_capability': '地点搜索', 'target_app': '高德地图'},
            {'scene': '出行导航', 'capability': '路线规划', 'sub_capability': '公交路线', 'target_app': '高德地图'},
            {'scene': '影音娱乐', 'capability': '内容播放', 'sub_capability': '视频搜索', 'target_app': '爱奇艺'},
            {'scene': '生活服务', 'capability': '餐饮外卖', 'sub_capability': '门店搜索', 'target_app': '美团'},
        ]
        pd.DataFrame([{**row, 'use_resource_prior': False, 'reference_example': '查找指定内容并查看详情'} for row in branches]).to_excel(kb / 'VLA场景树.xlsx', index=False)
        pd.DataFrame([{**row, 'sub_capability_desc': '使用搜索栏查找内容'} for row in branches]).to_excel(kb / 'APP操控先验知识库.xlsx', index=False)
        with pd.ExcelWriter(kb / 'APP资源先验知识库.xlsx') as writer:
            for name in ('高德地图', '爱奇艺', '美团'):
                pd.DataFrame([{'实体': '模拟参考内容'}]).to_excel(writer, sheet_name=name, index=False)
        model = DemoModel()
        manager = TaskGenerationJobManager(
            base / 'jobs', base / 'runs', base / 'exports', kb, base / 'logs',
            classification_runner=lambda seeds, **kwargs: run_augmentation_classification(seeds, **kwargs, model=model),
            generation_runner=lambda seeds, count, **kwargs: run_augmentation_generation(seeds, count, **kwargs, model=model),
        )
        rows = []
        for index, branch in enumerate((branches[0], branches[0], branches[2])):
            rows.append({**{key: branch[key] for key in ('scene', 'capability', 'sub_capability')}, 'app': branch['target_app'], 'task': f'查找指定内容并查看第 {index + 1} 个搜索结果'})
        rows.append({'app': '高德地图', 'task': '查看之前保存的地点列表', 'scene': '旧场景名称', 'capability': '旧能力名称', 'sub_capability': '旧任务类型'})
        rows.append({'app': '爱奇艺', 'task': '查看推荐页中感兴趣的内容', 'scene': 'Unclassified', 'capability': 'Unclassified', 'sub_capability': 'Unclassified'})
        seed_file = base / 'failed-seeds.xlsx'
        pd.DataFrame(rows).to_excel(seed_file, index=False)
        legacy = manager._new_job('augmentation', total_items=1, generate_n=1, input_filename='历史作业.xlsx')
        manager._finish(legacy['job_id'], {'results': [{
            'result_id': 'legacy-result', 'source_row': 2, 'source_task': '历史失败用例',
            'app': '美团', 'scene': '生活服务', 'capability': '餐饮外卖', 'sub_capability': '门店搜索',
            'task': '搜索附近的早餐店并查看营业时间', 'deleted': False,
        }], 'errors': []})
        job = manager.submit_augmentation(seed_file, '失败用例演示.xlsx', 2, auto_start=False)
        deadline = time.monotonic() + 15
        while manager.get(job['job_id'])['status'] in {'queued', 'running'} and time.monotonic() < deadline:
            time.sleep(0.05)
        configure_job_manager(manager)
        app = FastAPI(title='Augmentation acceptance: isolated fixtures, simulated model')
        app.include_router(router)
        app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='assets')

        @app.get('/{path:path}')
        def frontend(path: str):
            if path.startswith('api/'):
                raise HTTPException(404)
            return FileResponse(dist / 'index.html')

        try:
            uvicorn.run(app, host='127.0.0.1', port=args.port)
        finally:
            manager._executor.shutdown(wait=True, cancel_futures=True)


if __name__ == '__main__':
    main()
