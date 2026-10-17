// tcmcp-capture — native capture service for twincat-mcp recordings.
//
// One process per recording, controlled by the twincat-mcp server through JSON lines on stdin/stdout
// (stderr is free text for diagnostics):
//   -> {"cmd": "start", ...}   <- {"reply": "start", "ok": true, "subscribed": N, ...} | {"ok": false, "error": ...}
//   -> {"cmd": "status"}       <- {"reply": "status", ...}
//   -> {"cmd": "stop"}         <- {"reply": "stop", "ok": true, "footer": {...}}
//   <- {"event": "finished", "footer": {...}}   when a limit (duration, post-trigger, stop_when, max_events) ended it
// Closing stdin stops the recording cleanly (footer written) and exits.
//
// tcmcp-capture --version    prints {"name": "tcmcp-capture", "version": ..., "protocol": 1, "ads": ...}
// tcmcp-capture --selftest   checks SHA-256 / HMAC / base64 / JSON against known vectors
#include <cstdio>
#include <iostream>
#include <mutex>
#include <string>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

#include "engine.hpp"
#include "json.hpp"
#include "sha256.hpp"

#ifndef TCMCP_VERSION
#define TCMCP_VERSION "0.0.0-dev"
#endif

namespace {

const int kProtocol = 1;

const char* ads_flavour() {
#if defined(USE_TWINCAT_ROUTER) && defined(TCMCP_TCADSDLL_RUNTIME)
    return "Beckhoff AdsLib on TcAdsDll (TwinCAT router, loaded at run time)";
#elif defined(USE_TWINCAT_ROUTER)
    return "Beckhoff AdsLib on TcAdsDll (TwinCAT router, TwinCAT SDK build)";
#else
    return "Beckhoff AdsLib standalone (AMS/TCP router)";
#endif
}

std::string version_json() {
    tcmcp::JObj o;
    o.str("name", "tcmcp-capture").str("version", TCMCP_VERSION).i("protocol", kProtocol).str("ads", ads_flavour());
    o.raw("backends", "[\"ads\", \"sim\"]");
    return o.done();
}

int selftest() {
    int bad = 0;
    auto check = [&](bool ok, const char* what) {
        std::printf("%s %s\n", ok ? "ok  " : "FAIL", what);
        if (!ok) ++bad;
    };
    tcmcp::Sha256 a;
    a.update(std::string("abc"));
    check(a.hexdigest() == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "sha256(abc)");
    tcmcp::Sha256 e;
    check(e.hexdigest() == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "sha256('')");
    std::string k = "key";
    std::vector<uint8_t> key(k.begin(), k.end());
    check(tcmcp::hmac_sha256_hex(key, "The quick brown fox jumps over the lazy dog") ==
              "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8",
          "hmac-sha256 (RFC example)");
    const uint8_t m[] = {'f', 'o', 'o', 'b', 'a', 'r'};
    check(tcmcp::base64(m, 6) == "Zm9vYmFy" && tcmcp::base64(m, 5) == "Zm9vYmE=" && tcmcp::base64(m, 4) == "Zm9vYg==",
          "base64");
    try {
        auto j = tcmcp::json_parse("{\"a\": [1, -2, 1.5e3, \"x\\u00e9\\ud83d\\ude00\"], \"ft\": 133000000000000000}");
        check(j.get("a")->a[1].i == -2 && j.get("a")->a[2].d == 1500.0 &&
                  j.get("a")->a[3].s == "x\xC3\xA9\xF0\x9F\x98\x80" && j.integer("ft", 0) == 133000000000000000LL,
              "json");
    } catch (const std::exception& ex) {
        check(false, ex.what());
    }
    check(tcmcp::utc_iso_from_filetime(116444736000000000LL) == "1970-01-01T00:00:00+00:00" &&
              tcmcp::utc_iso_from_filetime(133000000001234560LL) == "2022-06-18T04:26:40.123456+00:00",
          "filetime -> utc");
    return bad ? 1 : 0;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc > 1) {
        std::string a = argv[1];
        if (a == "--version" || a == "-v") {
            std::printf("%s\n", version_json().c_str());
            return 0;
        }
        if (a == "--selftest") return selftest();
        std::fprintf(stderr, "usage: tcmcp-capture [--version | --selftest]  (control via JSON lines on stdin)\n");
        return 2;
    }
#ifdef _WIN32
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
#endif
    std::mutex out_mu;
    auto out = [&out_mu](const std::string& s) {
        std::lock_guard<std::mutex> lk(out_mu);
        std::fwrite(s.data(), 1, s.size(), stdout);
        std::fputc('\n', stdout);
        std::fflush(stdout);
    };
    tcmcp::Engine eng(TCMCP_VERSION);
    eng.emit = out;
    {
        tcmcp::JObj o;
        o.str("event", "ready").str("version", TCMCP_VERSION).i("protocol", kProtocol).str("ads", ads_flavour());
        out(o.done());
    }
    std::string line;
    while (std::getline(std::cin, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty()) continue;
        tcmcp::JVal cmd;
        try {
            cmd = tcmcp::json_parse(line);
        } catch (const std::exception& ex) {
            tcmcp::JObj o;
            o.str("reply", "error").b("ok", false).str("error", ex.what());
            out(o.done());
            continue;
        }
        std::string c = cmd.str("cmd");
        if (c == "start") {
            out(eng.start(cmd));
        } else if (c == "status") {
            if (!eng.started()) {
                tcmcp::JObj o;
                o.str("reply", "status").b("ok", false).str("error", "not started");
                out(o.done());
            } else {
                out(eng.status());
            }
        } else if (c == "stop") {
            out(eng.stop(cmd.str("reason", "requested")));
        } else if (c == "ping") {
            tcmcp::JObj o;
            o.str("reply", "ping").b("ok", true);
            out(o.done());
        } else if (c == "quit") {
            if (eng.running()) eng.stop(cmd.str("reason", "quit"));
            tcmcp::JObj o;
            o.str("reply", "quit").b("ok", true);
            out(o.done());
            return 0;
        } else {
            tcmcp::JObj o;
            o.str("reply", "error").b("ok", false).str("error", "unknown cmd '" + c + "'");
            out(o.done());
        }
    }
    // stdin closed: the MCP server went away — end the recording cleanly so the file is complete and signed
    if (eng.running()) eng.stop("stdin closed (MCP server gone)");
    return 0;
}
