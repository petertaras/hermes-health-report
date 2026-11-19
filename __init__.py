"""Hermes health report plugin entry point."""


def register(ctx):
    from .health import handler, setup_args
    from .maintenance import completed
    ctx.register_cli_command('health-report', 'Build a Hermes health report', setup_args, handler)
    ctx.register_hook('kanban_task_completed', completed)
