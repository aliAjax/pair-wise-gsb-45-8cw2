"""应用入口：参数解析、依赖组装与HTTP服务生命周期。"""
import argparse
from pathlib import Path

from src.audit import AuditRecorder
from src.http_api import create_server
from src.repository import Repository
from src.rules import DomainRules
from src.service import Service
from src.tug_repository import TugRepository
from src.tug_rules import TugRules
from src.tug_service import TugService


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "port-berth.db"
DEFAULT_PORT = 8321


def build_services(db_path: str):
    repository = Repository(db_path)
    tug_repository = TugRepository(db_path)
    audit = AuditRecorder(repository)
    tug_service = TugService(repository, tug_repository, TugRules())
    service = Service(repository, DomainRules(), audit, escort_hooks=tug_service)
    return service, tug_service


def build_service(db_path: str) -> Service:
    return build_services(db_path)[0]


def parse_args():
    parser = argparse.ArgumentParser(description="港口泊位与航道调度")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite数据库路径")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP监听端口")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    Path(args.db).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    service, tug_service = build_services(args.db)
    server = create_server(args.host, args.port, service, BASE_DIR / "static", tug_service)
    print("港口泊位与航道调度 listening on http://%s:%s" % (args.host, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
