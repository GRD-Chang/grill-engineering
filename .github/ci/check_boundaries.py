"""Check the capability boundaries used by GitHub observations and notices.

AST traversal includes imports inside functions. Keep the rules here beside CI,
so both typecheck and the ordinary product suite exercise the same contract.
"""
from __future__ import annotations

import ast
from pathlib import Path


RULES = {
    'adapters/github_commands.py': (
        ('agent_run.adapters.auth.native_probe', '通用命令应自行提取有界安全事实；认证 header 解析属于认证探针'),
    ),
    'adapters/github.py': (
        ('agent_run.adapters.remote', '读取不得获取业务 Effect 写能力；使用读取传输'),
        ('agent_run.adapters.operations', '读取不得经 NativeOperations 取得业务写能力'),
    ),
    'adapters/source.py': (
        ('agent_run.adapters.remote', 'Source 读取不得获取业务 Effect 写能力'),
        ('agent_run.adapters.operations', 'Source 读取不得经 NativeOperations 取得业务写能力'),
    ),
    'adapters/github_transport.py': (
        ('agent_run.adapters.remote', '物理读取不得依赖 Effect；使用 GitHubReadTransport'),
        ('agent_run.adapters.operations', '物理读取不得经 NativeOperations 取得业务写能力'),
        ('agent_run.core.kernel', '物理读取不得持有完整 Kernel'),
    ),
    'adapters/remote_observation.py': (
        ('agent_run.adapters.remote', '观察不得回调旧 Effect 协议；使用只读 GitHubReadTransport'),
        ('agent_run.adapters.operations', '观察不得经 NativeOperations 取得业务写能力'),
        ('agent_run.core.kernel', '观察应仅消费显式请求与只读物理能力'),
    ),
    'adapters/status_comment.py': (
        ('agent_run.adapters.remote', '评论传输应使用最小物理能力，恢复评论仍归 Effect'),
        ('agent_run.adapters.operations', '评论传输不得经 NativeOperations 取得业务写能力'),
        ('agent_run.core.kernel', '评论传输不得持有完整 Kernel'),
    ),
    'notifications/boundary_projection.py': (
        ('agent_run.adapters.remote', '通知投影不得获取业务 Effect 写能力'),
        ('agent_run.adapters.operations', '通知投影不得经 NativeOperations 取得业务写能力'),
        ('agent_run.core.kernel', '通知投影应消费只读事实，不持有完整 Kernel'),
    ),
    'notifications/status_comment.py': (
        ('agent_run.adapters.remote', 'Status 投递仅使用专属评论传输，不获取业务 Effect 写能力'),
        ('agent_run.adapters.operations', 'Status 投递不得经 NativeOperations 取得业务写能力'),
        ('agent_run.core.kernel', 'Status 应使用 StatusBinding 和自己的只读投影'),
    ),
    'notifications/sender.py': (
        ('agent_run.adapters.remote', '通知投递不得获取业务 Effect 写能力'),
        ('agent_run.adapters.operations', '通知投递不得经 NativeOperations 取得业务写能力'),
        ('agent_run.core.kernel', '通知应使用只读 StatusBinding，不得恢复完整 Kernel 绑定'),
    ),
}


def violations(source: str, module: str) -> list[str]:
    tree = ast.parse(source)
    failures = []
    for node in ast.walk(tree):
        imported: list[str] = []
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            package = module.removesuffix('.py').replace('/', '.')
            base = (node.module or '')
            if node.level:
                parents = ['agent_run', *package.split('.')[:-1]]
                base = '.'.join(parents[:len(parents) - node.level + 1] + ([base] if base else []))
            imported = [base, *(base + '.' + alias.name for alias in node.names)]
        for forbidden, guidance in RULES.get(module, ()):
            if any(name == forbidden or name.startswith(forbidden + '.') for name in imported):
                failures.append(f'{module}:{node.lineno}: {guidance} ({forbidden})')
    return failures


def check(root: Path) -> list[str]:
    failures = []
    for module in RULES:
        path = root / module
        if path.is_file():
            failures.extend(violations(path.read_text(encoding='utf-8'), module))
    return failures


if __name__ == '__main__':
    errors = check(Path(__file__).resolve().parents[2] / 'src' / 'agent_run')
    if errors:
        raise SystemExit('\n'.join(errors))
    print('GitHub/Status capability boundaries: OK')
