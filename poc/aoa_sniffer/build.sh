#!/usr/bin/env bash
# Manual build pipeline (no Gradle) for the AOA sniffer diagnostic app.
# Requires: JDK 17+, Android SDK with build-tools;34.0.0 and platforms;android-34.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANDROID_SDK="${ANDROID_SDK_ROOT:-/home/ubuntu/android-sdk}"
BUILD_TOOLS="$ANDROID_SDK/build-tools/34.0.0"
PLATFORM="$ANDROID_SDK/platforms/android-34"
BUILD="$HERE/build"

rm -rf "$BUILD"
mkdir -p "$BUILD/gen" "$BUILD/obj" "$BUILD/apk" "$BUILD/res_compiled"

echo "== aapt2 compile resources =="
"$BUILD_TOOLS/aapt2" compile --dir "$HERE/res" -o "$BUILD/res_compiled"

echo "== aapt2 link (generates R.java + base APK with resources/manifest) =="
"$BUILD_TOOLS/aapt2" link \
  -o "$BUILD/apk/base.apk" \
  -I "$PLATFORM/android.jar" \
  --manifest "$HERE/AndroidManifest.xml" \
  --java "$BUILD/gen" \
  --min-sdk-version 26 --target-sdk-version 34 \
  "$BUILD"/res_compiled/*.flat

echo "== javac =="
find "$HERE/src" "$BUILD/gen" -name '*.java' > "$BUILD/sources.txt"
javac -d "$BUILD/obj" -classpath "$PLATFORM/android.jar" -source 17 -target 17 @"$BUILD/sources.txt"

echo "== d8 (dex) =="
"$BUILD_TOOLS/d8" --output "$BUILD/apk" --lib "$PLATFORM/android.jar" $(find "$BUILD/obj" -name '*.class')

echo "== assemble unsigned apk =="
cp "$BUILD/apk/base.apk" "$BUILD/apk/unsigned.apk"
( cd "$BUILD/apk" && "$BUILD_TOOLS/aapt" add -f unsigned.apk classes.dex )

echo "== zipalign =="
"$BUILD_TOOLS/zipalign" -f -p 4 "$BUILD/apk/unsigned.apk" "$BUILD/apk/aligned.apk"

echo "== keystore (debug, generated once) =="
KEYSTORE="$HERE/debug.keystore"
if [[ ! -f "$KEYSTORE" ]]; then
  keytool -genkeypair -v -keystore "$KEYSTORE" -storepass android -alias androiddebugkey \
    -keypass android -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=O3RE, OU=O3RE, O=O3RE, L=Local, S=Local, C=US"
fi

echo "== apksigner =="
"$BUILD_TOOLS/apksigner" sign --ks "$KEYSTORE" --ks-pass pass:android \
  --out "$HERE/aoa_sniffer.apk" "$BUILD/apk/aligned.apk"

echo "== done: $HERE/aoa_sniffer.apk =="
