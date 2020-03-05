import contextlib
import functools
from dataclasses import dataclass
from typing import Optional, Dict, List, Union

from pytest_alembic.executor import CommandExecutor, ConnectionExecutor
from pytest_alembic.history import AlembicHistory


@contextlib.contextmanager
def runner(config, engine=None):
    """Manage the alembic execution context, in a given context.

    Yields:
        `MigrationContext` to the caller.
    """
    command_executor = CommandExecutor.from_config(config)
    migration_context = MigrationContext.from_config(config, command_executor)

    if engine:
        command_executor.configure(connection=engine)
        migration_context.connection_executor = ConnectionExecutor(engine)
        yield migration_context
    else:
        yield migration_context


@dataclass
class MigrationContext:
    """Within a given environment/execution context, executes alembic commands.
    """

    command_executor: CommandExecutor
    revision_upgrade_data: Dict[str, Union[Dict, List]]
    connection_executor: Optional[ConnectionExecutor] = None

    @classmethod
    def from_config(cls, config, command_executor):
        return cls(
            command_executor=command_executor,
            revision_upgrade_data=config.get("revision_upgrade_data", {}),
        )

    def generate_revision(self, process_revision_directives=None, **kwargs):
        """Generate a test revision.

        The final act of this process raises a `RevisionSuccess`, which is used as a sentinal
        to indicate the revision was generated successfully, while not actually finishing the
        generation of the revision file.
        """
        fn = RevisionSuccess.process_revision_directives(process_revision_directives)
        try:
            return self.command_executor.run_command(
                "revision", process_revision_directives=fn, **kwargs
            )
        except RevisionSuccess:
            pass

    @property
    def history(self) -> AlembicHistory:
        """Get the revision history.
        """
        raw_history = self.command_executor.run_command("history")
        return AlembicHistory.parse(tuple(raw_history))

    @property
    def heads(self) -> List[str]:
        """Get the list of revision heads.
        """
        return self.command_executor.run_command("heads")

    @property
    def current(self) -> Optional[str]:
        """Get the list of revision heads.
        """
        current = self.command_executor.run_command("current")
        if current:
            return current[0].strip()
        return "base"

    def raw_command(self, *args, **kwargs):
        """Execute a raw alembic command.
        """
        return self.command_executor.run_command(*args, **kwargs)

    def managed_upgrade(self, dest_revision):
        current = self.current
        for next_revision in self.history.revision_range(current, dest_revision):
            upgrade_data = self.revision_upgrade_data.get(next_revision)
            if upgrade_data:
                self.connection_executor.table_insert(next_revision, upgrade_data)
            self.raw_command("upgrade", next_revision)

    def migrate_up_before(self, revision):
        """Upgrade up to, but not including the given `revision`.
        """
        preceeding_revision = self.history.previous_revision(revision)
        self.managed_upgrade(preceeding_revision)

    def migrate_up_to(self, revision):
        """Upgrade up to, and including the given `revision`.
        """
        self.managed_upgrade(revision)

    def migrate_up_one(self):
        """Upgrade up by exactly one revision.
        """
        revision = self.history.next_revision(self.current)
        self.managed_upgrade(revision)

    def migrate_down_before(self, revision):
        """Upgrade down to, but not including the given `revision`.
        """
        next_revision = self.history.next_revision(revision)
        self.raw_command("downgrade", self.get_hash_before(next_revision))

    def migrate_down_to(self, revision):
        """Upgrade down to, and including the given `revision`.
        """
        self.history.validate_revision(revision)
        self.raw_command("downgrade", revision)

    def migrate_down_one(self):
        """Upgrade down by exactly one revision.
        """
        self.raw_command("downgrade", "-1")

    def roundtrip_next_revision(self):
        """Upgrade, downgrade then upgrade.

        This is meant to ensure that the given revision is idempotent.
        """
        self.migrate_up_one()
        self.migrate_down_one()
        self.migrate_up_one()

    def insert_into(self, table, data):
        """
        """
        return self.connection_executor(revision=self.current, tablename=table, data=data)


class RevisionSuccess(Exception):
    """Raise when a revision is successfully generated.

    In order to prevent the generation of an actual revision file on disk when running tests,
    this exception should be raised.
    """

    @classmethod
    def process_revision_directives(cls, fn=None):
        """Wrap a real `process_revision_directives` function, while preventing it from completing.
        """

        @functools.wraps(fn)
        def _process_revision_directives(context, revision, directives):
            if fn:
                fn(context, revision, directives)
            raise cls()

        return _process_revision_directives
