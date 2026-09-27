// Only these public assets may be deployed. Credentials and probe evidence stay local.
import {copyFileSync,cpSync,mkdirSync,writeFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {resolve} from 'node:path';
const root=fileURLToPath(new URL('..',import.meta.url)),out=resolve(root,'.local/public-site');
mkdirSync(out,{recursive:true});
copyFileSync(resolve(root,'index.html'),resolve(out,'index.html'));
cpSync(resolve(root,'assets'),resolve(out,'assets'),{recursive:true});
writeFileSync(resolve(out,'_headers'),'/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n');
console.log('Public assets staged: index.html and assets/ only.');
