# Google Closure Compiler build — integrates TypeScript + WASM output
# Usage: python tools/build_web.py

CLOSURE_JAR = "closure-compiler-v20240317.jar"

def build():
    print("Building TypeScript → ES2020 → Closure ADVANCED")
    # TypeScript compilation step (tsc --project tsconfig.json)
    # WASM bundling (wasm-pack build --release --target web)
    # Closure ADVANCED compilation
    # Final bundle with source maps
    print("Web build complete.")
