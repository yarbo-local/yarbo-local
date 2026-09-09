# Vendored front-end dependencies

`lit.js` is Lit 3.3.3 (BSD-3-Clause, Google LLC) bundled into one ES module with
esbuild so the Studio loads nothing from the internet:

```bash
npm i lit@3 esbuild
echo "export * from 'lit'; export {repeat} from 'lit/directives/repeat.js'; export {classMap} from 'lit/directives/class-map.js';" > entry.js
npx esbuild entry.js --bundle --format=esm --minify --outfile=lit.js
```

License text: https://github.com/lit/lit/blob/main/LICENSE
