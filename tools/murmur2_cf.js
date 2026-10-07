/*
 * CurseForge MurmurHash2 fingerprint — an independent implementation, for cross-checking.
 *
 * This file exists so the plugin's Python implementation in
 * ``mod_update_checker/fingerprint.py`` can be verified against something written from a
 * different source, in a different language, without needing a CurseForge API key.
 *
 * Provenance: transcribed from the C# reference client
 * ``CurseForge.APIClient/Murmur2.cs`` (the .NET client published within CurseForge's own
 * ecosystem), which implements the algorithm described in the CurseForge API docs.
 * ``Math.imul`` is used for the 32-bit multiplies because JavaScript's ``*`` would lose the
 * low bits above 2^53.
 *
 * Usage:
 *
 *     echo '{"data": [1, 2, 3], "seed": 1}' | node tools/murmur2_cf.js
 *
 * Reads one JSON object from stdin (``data`` as an array of byte values) and prints the
 * fingerprint as a decimal integer.
 */

"use strict";

const M = 0x5bd1e995;
const R = 24;

function isWhitespace(b) {
    return b === 9 || b === 10 || b === 13 || b === 32;
}

function normalise(data) {
    return data.filter((b) => !isWhitespace(b));
}

function hash(data, seed) {
    const bytes = normalise(data);
    const length = bytes.length;
    if (length === 0) {
        return 0;
    }

    let h = (seed ^ length) >>> 0;
    let index = 0;
    let remaining = length;

    while (remaining >= 4) {
        let k = (bytes[index] | (bytes[index + 1] << 8) | (bytes[index + 2] << 16) |
            (bytes[index + 3] << 24)) >>> 0;
        k = Math.imul(k, M) >>> 0;
        k = (k ^ (k >>> R)) >>> 0;
        k = Math.imul(k, M) >>> 0;
        h = Math.imul(h, M) >>> 0;
        h = (h ^ k) >>> 0;
        index += 4;
        remaining -= 4;
    }

    if (remaining === 3) {
        h = (h ^ (bytes[index + 2] << 16)) >>> 0;
    }
    if (remaining >= 2) {
        h = (h ^ (bytes[index + 1] << 8)) >>> 0;
    }
    if (remaining >= 1) {
        h = (h ^ bytes[index]) >>> 0;
        h = Math.imul(h, M) >>> 0;
    }

    h = (h ^ (h >>> 13)) >>> 0;
    h = Math.imul(h, M) >>> 0;
    h = (h ^ (h >>> 15)) >>> 0;
    return h >>> 0;
}

function main() {
    let raw = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
        raw += chunk;
    });
    process.stdin.on("end", () => {
        const payload = JSON.parse(raw);
        const seed = payload.seed === undefined ? 1 : payload.seed;
        process.stdout.write(String(hash(payload.data, seed)));
    });
}

main();
