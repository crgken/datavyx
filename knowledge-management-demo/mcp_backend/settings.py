"""Only MCP, identity and database configuration. No legacy env loading."""
from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit
from .errors import require


@dataclass(frozen=True)
class Settings:
    uri: str
    database: str
    user: str
    password: str = field(repr=False)
    issuer: str
    audience: str
    clients: tuple[str, ...]
    resource_url: str
    cursor_secret: str = field(repr=False)
    host: str = '127.0.0.1'
    port: int = 8098
    mode: str = 'production'
    max_records: int = 10000
    organization: str = ''
    recording_enabled: bool = False
    recording_actors: tuple[str, ...] = ()
    purge_ledger_dir: str = ''

    def __post_init__(self):
        uri = urlsplit(self.uri)
        require(uri.scheme in {'bolt','neo4j','bolt+s','neo4j+s'} and uri.hostname
                and not uri.username and not uri.password and not uri.query and not uri.fragment,
                'INVALID_CONFIGURATION','Invalid database URI.')
        require(self.mode in {'production','test'} and self.host in {'127.0.0.1','localhost'}
                and 1024 <= self.port <= 65535 and 100 <= self.max_records <= 100000,
                'INVALID_CONFIGURATION','Invalid runtime limits or listener.')
        if self.mode == 'test':
            require(uri.hostname in {'127.0.0.1','localhost'} and uri.port == 17694,
                    'INVALID_CONFIGURATION','Tests require the isolated loopback database on 17694.')
        require(bool(self.database) and bool(self.user) and (bool(self.password) or self.mode=='test'),
                'INVALID_CONFIGURATION','Database credentials are required.')
        issuer, resource = urlsplit(self.issuer), urlsplit(self.resource_url)
        require(issuer.scheme=='https' and issuer.hostname and not issuer.username and not issuer.password
                and not issuer.query and not issuer.fragment and self.issuer.endswith('/'),
                'INVALID_CONFIGURATION','A canonical HTTPS issuer is required.')
        require(resource.hostname and not resource.username and not resource.password and not resource.query
                and not resource.fragment and (resource.scheme=='https' or self.mode=='test'
                    and resource.scheme=='http' and resource.hostname in {'127.0.0.1','localhost'}),
                'INVALID_CONFIGURATION','A canonical MCP resource URL is required.')
        require(bool(self.audience) and bool(self.clients) and all(self.clients) and len(self.cursor_secret)>=32,
                'INVALID_CONFIGURATION','Audience, client allowlist and a cursor signing secret are required.')
        require(self.mode=='test' or self.audience==self.resource_url,
                'INVALID_CONFIGURATION','Production audience must identify this canonical MCP resource.')
        require(type(self.recording_enabled) is bool, 'INVALID_CONFIGURATION', 'Recording mode must be boolean.')
        require(self.mode=='test' or bool(self.purge_ledger_dir) and Path(self.purge_ledger_dir).is_absolute(),
                'INVALID_CONFIGURATION','Production requires an explicit persistent erasure ledger directory.')

    @classmethod
    def from_env(cls):
        try:
            recording = os.getenv('MCP_RECORDING_ENABLED','0')
            require(recording in {'0','1'}, 'INVALID_CONFIGURATION', 'MCP_RECORDING_ENABLED must be 0 or 1.')
            return cls(uri=os.environ['MCP_NEO4J_URI'],database=os.getenv('MCP_NEO4J_DATABASE','neo4j'),
                user=os.getenv('MCP_NEO4J_USER','neo4j'),password=os.environ['MCP_NEO4J_PASSWORD'],
                issuer=os.environ['MCP_AUTH_ISSUER'],audience=os.environ['MCP_AUTH_AUDIENCE'],
                clients=tuple(s.strip() for s in os.environ['MCP_AUTH_CLIENT_IDS'].split(',')),
                resource_url=os.environ['MCP_RESOURCE_URL'],cursor_secret=os.environ['MCP_CURSOR_SECRET'],
                host=os.getenv('MCP_HOST','127.0.0.1'),port=int(os.getenv('MCP_PORT','8098')),
                mode=os.getenv('MCP_MODE','production'),max_records=int(os.getenv('MCP_MAX_RECORDS','10000')),
                organization=os.getenv('MCP_AUTH_ORGANIZATION',''), recording_enabled=recording=='1',
                recording_actors=tuple(s.strip() for s in os.getenv('MCP_RECORDING_ACTORS','').split(',') if s.strip()),
                purge_ledger_dir=os.getenv('MCP_PURGE_LEDGER_DIR',''))
        except (KeyError, ValueError):
            from .errors import KnowledgeError
            raise KnowledgeError('INVALID_CONFIGURATION','Required MCP configuration is missing or invalid.') from None
