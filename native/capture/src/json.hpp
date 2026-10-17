// Minimal JSON for the control channel (one command per line) and for the footer/status replies.
// Parses the full JSON grammar (RFC 8259) including \uXXXX escapes and surrogate pairs; integers are kept
// exactly as int64/uint64 when they fit (FILETIMEs, AMS ports), everything else as double.
#pragma once
#include <cerrno>
#include <cmath>
#include <cstdlib>
#include <cstdint>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace tcmcp {

struct JVal {
    enum Type { Null, Bool, Num, Str, Arr, Obj } t = Null;
    bool b = false;
    double d = 0;
    bool is_int = false;   // the literal was an integer that fits int64 (or uint64 when is_uint)
    bool is_uint = false;
    int64_t i = 0;
    uint64_t u = 0;
    std::string s;
    std::vector<JVal> a;
    std::vector<std::pair<std::string, JVal>> o;

    const JVal* get(const std::string& k) const {
        if (t != Obj) return nullptr;
        for (auto& kv : o)
            if (kv.first == k) return &kv.second;
        return nullptr;
    }
    bool has(const std::string& k) const {
        const JVal* v = get(k);
        return v && v->t != Null;
    }
    double num(const std::string& k, double def) const {
        const JVal* v = get(k);
        return (v && v->t == Num) ? v->d : def;
    }
    int64_t integer(const std::string& k, int64_t def) const {
        const JVal* v = get(k);
        if (!v || v->t != Num) return def;
        if (v->is_int) return v->i;
        return static_cast<int64_t>(std::llround(v->d));
    }
    std::string str(const std::string& k, const std::string& def = "") const {
        const JVal* v = get(k);
        return (v && v->t == Str) ? v->s : def;
    }
    bool boolean(const std::string& k, bool def) const {
        const JVal* v = get(k);
        return (v && v->t == Bool) ? v->b : def;
    }
};

class JParser {
public:
    explicit JParser(const std::string& s) : s_(s) {}
    JVal parse() {
        JVal v = value();
        ws();
        if (p_ != s_.size()) fail("trailing characters");
        return v;
    }

private:
    const std::string& s_;
    size_t p_ = 0;
    int depth_ = 0;

    [[noreturn]] void fail(const char* what) {
        throw std::runtime_error(std::string("JSON: ") + what + " at offset " + std::to_string(p_));
    }
    void ws() {
        while (p_ < s_.size() && (s_[p_] == ' ' || s_[p_] == '\t' || s_[p_] == '\n' || s_[p_] == '\r')) ++p_;
    }
    bool lit(const char* w) {
        size_t n = std::char_traits<char>::length(w);
        if (s_.compare(p_, n, w) == 0) { p_ += n; return true; }
        return false;
    }
    JVal value() {
        ws();
        if (p_ >= s_.size()) fail("unexpected end");
        if (++depth_ > 200) fail("nesting too deep");
        JVal v;
        char c = s_[p_];
        if (c == '{') {
            v.t = JVal::Obj; ++p_; ws();
            if (p_ < s_.size() && s_[p_] == '}') { ++p_; --depth_; return v; }
            for (;;) {
                ws();
                if (p_ >= s_.size() || s_[p_] != '"') fail("expected key");
                std::string k = string();
                ws();
                if (p_ >= s_.size() || s_[p_] != ':') fail("expected ':'");
                ++p_;
                v.o.emplace_back(std::move(k), value());
                ws();
                if (p_ < s_.size() && s_[p_] == ',') { ++p_; continue; }
                if (p_ < s_.size() && s_[p_] == '}') { ++p_; break; }
                fail("expected ',' or '}'");
            }
        } else if (c == '[') {
            v.t = JVal::Arr; ++p_; ws();
            if (p_ < s_.size() && s_[p_] == ']') { ++p_; --depth_; return v; }
            for (;;) {
                v.a.push_back(value());
                ws();
                if (p_ < s_.size() && s_[p_] == ',') { ++p_; continue; }
                if (p_ < s_.size() && s_[p_] == ']') { ++p_; break; }
                fail("expected ',' or ']'");
            }
        } else if (c == '"') {
            v.t = JVal::Str; v.s = string();
        } else if (lit("true")) { v.t = JVal::Bool; v.b = true; }
        else if (lit("false")) { v.t = JVal::Bool; v.b = false; }
        else if (lit("null")) { v.t = JVal::Null; }
        else number(v);
        --depth_;
        return v;
    }
    static void utf8(std::string& out, uint32_t cp) {
        if (cp < 0x80) out += char(cp);
        else if (cp < 0x800) { out += char(0xC0 | (cp >> 6)); out += char(0x80 | (cp & 0x3F)); }
        else if (cp < 0x10000) { out += char(0xE0 | (cp >> 12)); out += char(0x80 | ((cp >> 6) & 0x3F)); out += char(0x80 | (cp & 0x3F)); }
        else { out += char(0xF0 | (cp >> 18)); out += char(0x80 | ((cp >> 12) & 0x3F)); out += char(0x80 | ((cp >> 6) & 0x3F)); out += char(0x80 | (cp & 0x3F)); }
    }
    uint32_t hex4() {
        if (p_ + 4 > s_.size()) fail("short \\u escape");
        uint32_t v = 0;
        for (int k = 0; k < 4; ++k) {
            char h = s_[p_++];
            v <<= 4;
            if (h >= '0' && h <= '9') v |= uint32_t(h - '0');
            else if (h >= 'a' && h <= 'f') v |= uint32_t(h - 'a' + 10);
            else if (h >= 'A' && h <= 'F') v |= uint32_t(h - 'A' + 10);
            else fail("bad \\u escape");
        }
        return v;
    }
    std::string string() {
        ++p_;  // opening quote
        std::string out;
        for (;;) {
            if (p_ >= s_.size()) fail("unterminated string");
            char c = s_[p_++];
            if (c == '"') break;
            if (static_cast<unsigned char>(c) < 0x20) fail("control character in string");
            if (c != '\\') { out += c; continue; }
            if (p_ >= s_.size()) fail("unterminated escape");
            char e = s_[p_++];
            switch (e) {
                case '"': out += '"'; break;
                case '\\': out += '\\'; break;
                case '/': out += '/'; break;
                case 'b': out += '\b'; break;
                case 'f': out += '\f'; break;
                case 'n': out += '\n'; break;
                case 'r': out += '\r'; break;
                case 't': out += '\t'; break;
                case 'u': {
                    uint32_t cp = hex4();
                    if (cp >= 0xD800 && cp <= 0xDBFF && p_ + 6 <= s_.size() && s_[p_] == '\\' && s_[p_ + 1] == 'u') {
                        size_t save = p_;
                        p_ += 2;
                        uint32_t lo = hex4();
                        if (lo >= 0xDC00 && lo <= 0xDFFF) cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        else p_ = save;
                    }
                    utf8(out, cp);
                    break;
                }
                default: fail("bad escape");
            }
        }
        return out;
    }
    void number(JVal& v) {
        size_t st = p_;
        if (p_ < s_.size() && s_[p_] == '-') ++p_;
        bool digits = false, frac = false;
        while (p_ < s_.size() && s_[p_] >= '0' && s_[p_] <= '9') { ++p_; digits = true; }
        if (p_ < s_.size() && s_[p_] == '.') { frac = true; ++p_; while (p_ < s_.size() && s_[p_] >= '0' && s_[p_] <= '9') ++p_; }
        if (p_ < s_.size() && (s_[p_] == 'e' || s_[p_] == 'E')) {
            frac = true; ++p_;
            if (p_ < s_.size() && (s_[p_] == '+' || s_[p_] == '-')) ++p_;
            while (p_ < s_.size() && s_[p_] >= '0' && s_[p_] <= '9') ++p_;
        }
        if (!digits) fail("bad value");
        std::string tok = s_.substr(st, p_ - st);
        v.t = JVal::Num;
        v.d = std::strtod(tok.c_str(), nullptr);
        if (!frac) {
            errno = 0;
            if (tok[0] == '-') {
                long long x = std::strtoll(tok.c_str(), nullptr, 10);
                if (errno == 0) { v.is_int = true; v.i = x; v.u = static_cast<uint64_t>(x); }
            } else {
                unsigned long long x = std::strtoull(tok.c_str(), nullptr, 10);
                if (errno == 0) {
                    v.is_uint = true; v.u = x;
                    if (x <= 0x7FFFFFFFFFFFFFFFull) { v.is_int = true; v.i = static_cast<int64_t>(x); }
                }
            }
        }
    }
};

inline JVal json_parse(const std::string& s) { return JParser(s).parse(); }

// ------------------------------------------------------------------ output helpers
inline std::string jq(const std::string& s) {
    std::string o;
    o.reserve(s.size() + 2);
    o += '"';
    for (unsigned char c : s) {
        switch (c) {
            case '"': o += "\\\""; break;
            case '\\': o += "\\\\"; break;
            case '\n': o += "\\n"; break;
            case '\r': o += "\\r"; break;
            case '\t': o += "\\t"; break;
            default:
                if (c < 0x20) { char b[8]; std::snprintf(b, sizeof b, "\\u%04x", c); o += b; }
                else o += char(c);
        }
    }
    o += '"';
    return o;
}

inline std::string jnum(double v, int sig = 10) {
    if (!std::isfinite(v)) return "null";
    char b[40];
    std::snprintf(b, sizeof b, "%.*g", sig, v);
    return b;
}

// Builds one JSON object in Python's json.dumps style (", " and ": " separators).
class JObj {
public:
    JObj& raw(const std::string& k, const std::string& json_text) {
        sep();
        s_ += jq(k);
        s_ += ": ";
        s_ += json_text;
        return *this;
    }
    JObj& str(const std::string& k, const std::string& v) { return raw(k, jq(v)); }
    JObj& i(const std::string& k, long long v) { return raw(k, std::to_string(v)); }
    JObj& u(const std::string& k, unsigned long long v) { return raw(k, std::to_string(v)); }
    JObj& d(const std::string& k, double v, int sig = 10) { return raw(k, jnum(v, sig)); }
    JObj& b(const std::string& k, bool v) { return raw(k, v ? "true" : "false"); }
    JObj& null(const std::string& k) { return raw(k, "null"); }
    std::string done() const { return "{" + s_ + "}"; }

private:
    void sep() {
        if (!s_.empty()) s_ += ", ";
    }
    std::string s_;
};

inline std::string jarr(const std::vector<std::string>& items) {
    std::string s = "[";
    for (size_t k = 0; k < items.size(); ++k) {
        if (k) s += ", ";
        s += items[k];
    }
    return s + "]";
}

}  // namespace tcmcp
