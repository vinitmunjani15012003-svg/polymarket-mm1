from py_clob_client.client import ClobClient
import inspect

print("Testing post_order signature in py_clob_client")
sig = inspect.signature(ClobClient.post_order)
print(f"Signature: {sig}")

if "post_only" in sig.parameters:
    print("SUCCESS: post_only is supported!")
else:
    print("WARNING: post_only not in explicit signature. Does it use kwargs?")
    
print("Checking for kwargs:", any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()))
