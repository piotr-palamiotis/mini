"""Helpers shared by the git capabilities."""
from ..harness import checkpoint as ckpt


def repo(agent):
    """The workspace when it is a git repository; ckpt.GitError otherwise."""
    if not agent.workspace or not ckpt.is_repo(agent.workspace):
        raise ckpt.GitError(ckpt.guard_note(agent.workspace or agent.folder))
    return agent.workspace


def git(agent, *args):
    return ckpt.git(repo(agent), *args)
