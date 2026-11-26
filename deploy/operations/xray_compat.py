#!/usr/local/bin/python3
import os,sys,subprocess,re
binary='/usr/local/bin/xray-current'
if len(sys.argv)>1 and sys.argv[1]=='x25519':
 r=subprocess.run([binary]+sys.argv[1:],capture_output=True,text=True)
 if r.returncode:sys.stderr.write(r.stderr);sys.exit(r.returncode)
 private=re.search(r'(?:PrivateKey|Private key):\s*(\S+)',r.stdout)
 public=re.search(r'(?:Password(?: \(PublicKey\))?|PublicKey|Public key):\s*(\S+)',r.stdout)
 if not private or not public:sys.exit('Unexpected x25519 output')
 print('Private key: '+private.group(1));print('Public key: '+public.group(1))
else:os.execv(binary,[binary]+sys.argv[1:])
