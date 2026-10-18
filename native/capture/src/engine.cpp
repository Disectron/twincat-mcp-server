#include "engine.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <fstream>
#include <limits>
#include <sstream>

namespace tcmcp {

static constexpr int64_t kFtUnixEpoch = 116444736000000000LL;   // FILETIME of 1970-01-01

// ---------------------------------------------------------------- time formatting
static int64_t floor_div(int64_t a, int64_t b) {
    int64_t q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0))) --q;
    return q;
}

// days since 1970-01-01 -> civil date (Howard Hinnant's algorithm)
static void civil_from_days(int64_t z, int& y, unsigned& m, unsigned& d) {
    z += 719468;
    const int64_t era = (z >= 0 ? z : z - 146096) / 146097;
    const unsigned doe = static_cast<unsigned>(z - era * 146097);
    const unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const int64_t yy = static_cast<int64_t>(yoe) + era * 400;
    const unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const unsigned mp = (5 * doy + 2) / 153;
    d = doy - (153 * mp + 2) / 5 + 1;
    m = mp < 10 ? mp + 3 : mp - 9;
    y = static_cast<int>(yy + (m <= 2));
}

// Python's datetime.isoformat() for an aware UTC time: microseconds only when non-zero.
std::string utc_iso_from_filetime(int64_t ft) {
    int64_t u100 = ft - kFtUnixEpoch;
    int64_t secs = floor_div(u100, 10000000);
    int64_t micro = (u100 - secs * 10000000) / 10;
    int64_t days = floor_div(secs, 86400);
    int64_t sod = secs - days * 86400;
    int y;
    unsigned mo, d;
    civil_from_days(days, y, mo, d);
    char b[64];
    if (micro)
        std::snprintf(b, sizeof b, "%04d-%02u-%02uT%02d:%02d:%02d.%06d+00:00", y, mo, d, int(sod / 3600),
                      int(sod / 60 % 60), int(sod % 60), int(micro));
    else
        std::snprintf(b, sizeof b, "%04d-%02u-%02uT%02d:%02d:%02d+00:00", y, mo, d, int(sod / 3600),
                      int(sod / 60 % 60), int(sod % 60));
    return b;
}

std::string utc_iso_now() {
    using namespace std::chrono;
    auto us = duration_cast<microseconds>(system_clock::now().time_since_epoch()).count();
    return utc_iso_from_filetime(kFtUnixEpoch + us * 10);
}

std::string Engine::ms(int64_t ft) const {
    int64_t d = ft - t0_;
    bool neg = d < 0;
    uint64_t a = neg ? static_cast<uint64_t>(-(d + 1)) + 1 : static_cast<uint64_t>(d);
    char b[48];
    std::snprintf(b, sizeof b, "%s%llu.%04llu", neg ? "-" : "", static_cast<unsigned long long>(a / 10000),
                  static_cast<unsigned long long>(a % 10000));
    return b;
}

// ---------------------------------------------------------------- conditions
static size_t type_size(const std::string& t) {
    if (t == "bool" || t == "u8" || t == "i8") return 1;
    if (t == "u16" || t == "i16") return 2;
    if (t == "u32" || t == "i32" || t == "f32") return 4;
    if (t == "u64" || t == "i64" || t == "f64") return 8;
    return 0;
}

bool Cond::parse(const JVal& j, size_t, const std::vector<BlockSpec>& blocks, std::string& err) {
    if (j.t != JVal::Obj) return true;   // null: no condition
    int64_t bid = j.integer("block", -1);
    size_t idx = blocks.size();
    for (size_t k = 0; k < blocks.size(); ++k)
        if (static_cast<int64_t>(blocks[k].id) == bid) idx = k;
    if (idx == blocks.size()) {
        err = "condition block " + std::to_string(bid) + " is not in blocks";
        return false;
    }
    type = j.str("type");
    op = j.str("op");
    size_t ts = type_size(type);
    if (!ts) {
        err = "condition type '" + type + "' (native conditions: bool, u8..u64, i8..i64, f32, f64)";
        return false;
    }
    if (op != "==" && op != "!=" && op != ">" && op != ">=" && op != "<" && op != "<=") {
        err = "condition op '" + op + "'";
        return false;
    }
    off = static_cast<uint32_t>(j.integer("off", 0));
    if (off + ts > blocks[idx].size) {
        err = "condition offset outside block " + std::to_string(bid);
        return false;
    }
    const JVal* v = j.get("value");
    if (!v || (v->t != JVal::Num && v->t != JVal::Bool)) {
        err = "condition value must be a number or boolean";
        return false;
    }
    value = *v;
    if (value.t == JVal::Bool) {
        value.t = JVal::Num;
        value.is_int = value.is_uint = true;
        value.i = value.b ? 1 : 0;
        value.u = static_cast<uint64_t>(value.i);
        value.d = static_cast<double>(value.i);
    }
    edge = j.boolean("edge", false);
    text = j.str("text");
    block = static_cast<uint32_t>(idx);
    set = true;
    return true;
}

namespace {
struct Num {
    enum K { S, U, F } k;
    int64_t s;
    uint64_t u;
    double f;
};
int cmp(const Num& a, const Num& b) {
    if (a.k == Num::F || b.k == Num::F) {
        double x = a.k == Num::F ? a.f : a.k == Num::S ? double(a.s) : double(a.u);
        double y = b.k == Num::F ? b.f : b.k == Num::S ? double(b.s) : double(b.u);
        if (std::isnan(x) || std::isnan(y)) return 2;   // unordered
        return x < y ? -1 : x > y ? 1 : 0;
    }
    if (a.k == Num::S && b.k == Num::S) return a.s < b.s ? -1 : a.s > b.s ? 1 : 0;
    if (a.k == Num::U && b.k == Num::U) return a.u < b.u ? -1 : a.u > b.u ? 1 : 0;
    if (a.k == Num::S) {   // S vs U
        if (a.s < 0) return -1;
        uint64_t x = static_cast<uint64_t>(a.s);
        return x < b.u ? -1 : x > b.u ? 1 : 0;
    }
    if (b.s < 0) return 1;   // U vs S
    uint64_t y = static_cast<uint64_t>(b.s);
    return a.u < y ? -1 : a.u > y ? 1 : 0;
}
}  // namespace

bool Cond::test(const uint8_t* d, uint32_t n) const {
    size_t ts = type_size(type);
    if (off + ts > n) return false;
    const uint8_t* p = d + off;
    Num a{};
    if (type == "bool") { a.k = Num::U; a.u = p[0] ? 1 : 0; }
    else if (type == "u8") { a.k = Num::U; a.u = p[0]; }
    else if (type == "i8") { int8_t x; std::memcpy(&x, p, 1); a.k = Num::S; a.s = x; }
    else if (type == "u16") { uint16_t x; std::memcpy(&x, p, 2); a.k = Num::U; a.u = x; }
    else if (type == "i16") { int16_t x; std::memcpy(&x, p, 2); a.k = Num::S; a.s = x; }
    else if (type == "u32") { uint32_t x; std::memcpy(&x, p, 4); a.k = Num::U; a.u = x; }
    else if (type == "i32") { int32_t x; std::memcpy(&x, p, 4); a.k = Num::S; a.s = x; }
    else if (type == "u64") { uint64_t x; std::memcpy(&x, p, 8); a.k = Num::U; a.u = x; }
    else if (type == "i64") { int64_t x; std::memcpy(&x, p, 8); a.k = Num::S; a.s = x; }
    else if (type == "f32") { float x; std::memcpy(&x, p, 4); a.k = Num::F; a.f = x; }
    else if (type == "f64") { double x; std::memcpy(&x, p, 8); a.k = Num::F; a.f = x; }
    else return false;
    Num b{};
    if (value.is_int) { b.k = Num::S; b.s = value.i; }
    else if (value.is_uint) { b.k = Num::U; b.u = value.u; }
    else { b.k = Num::F; b.f = value.d; }
    int c = cmp(a, b);
    if (c == 2) return op == "!=";
    if (op == "==") return c == 0;
    if (op == "!=") return c != 0;
    if (op == ">") return c > 0;
    if (op == ">=") return c >= 0;
    if (op == "<") return c < 0;
    return c <= 0;
}

bool Cond::fires(const uint8_t* d, uint32_t n) {
    bool now = test(d, n);
    if (!edge) return now;
    int prev_ = prev;
    prev = now ? 1 : 0;
    return now && prev_ == 0;
}

// ---------------------------------------------------------------- engine
Engine::~Engine() {
    if (started_ && !finished_.load()) stop("engine destroyed");
    if (writer_.joinable()) writer_.join();
}

void Engine::on_sample(uint32_t block, int64_t filetime, const uint8_t* data, uint32_t n) {
    if (!accepting_.load(std::memory_order_acquire)) {
        after_unsubscribe_.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    if (block < blocks_.size()) blocks_[block]->ring.push(filetime, data, n);
}

static bool read_key(const std::string& path, std::vector<uint8_t>& key, std::string& err) {
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        err = "cannot read the recording key " + path;
        return false;
    }
    std::string s((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    std::string hex;
    for (char c : s)
        if (!std::isspace(static_cast<unsigned char>(c))) hex += c;
    if (hex.empty() || hex.size() % 2) {
        err = "recording key " + path + " is not hex";
        return false;
    }
    key.clear();
    for (size_t i = 0; i < hex.size(); i += 2) {
        int v = 0;
        for (int k = 0; k < 2; ++k) {
            char c = hex[i + k];
            v <<= 4;
            if (c >= '0' && c <= '9') v |= c - '0';
            else if (c >= 'a' && c <= 'f') v |= c - 'a' + 10;
            else if (c >= 'A' && c <= 'F') v |= c - 'A' + 10;
            else {
                err = "recording key " + path + " is not hex";
                return false;
            }
        }
        key.push_back(static_cast<uint8_t>(v));
    }
    return true;
}

static std::string fail_reply(const std::string& msg, int block_id = -1, long code = 0) {
    JObj o;
    o.str("reply", "start").b("ok", false).str("error", msg);
    if (block_id >= 0) o.i("block", block_id);
    if (code) o.i("code", code);
    return o.done();
}

std::string Engine::start(const JVal& cmd) {
    if (started_) return fail_reply("this capture process already ran a recording (one process per recording)");
    // ------------------------------------------------ blocks
    const JVal* jb = cmd.get("blocks");
    if (!jb || jb->t != JVal::Arr || jb->a.empty()) return fail_reply("blocks: non-empty list required");
    std::vector<BlockSpec> specs;
    for (auto& b : jb->a) {
        BlockSpec s;
        s.id = static_cast<uint32_t>(b.integer("id", static_cast<int64_t>(specs.size())));
        s.igrp = static_cast<uint32_t>(b.integer("igrp", 0));
        s.ioffs = static_cast<uint32_t>(b.integer("ioffs", 0));
        int64_t sz = b.integer("size", 0);
        if (sz <= 0 || sz > (1 << 20)) return fail_reply("block " + std::to_string(s.id) + ": size must be 1..1048576");
        s.size = static_cast<uint32_t>(sz);
        s.leaves = static_cast<uint32_t>(std::max<int64_t>(1, b.integer("leaves", 1)));
        specs.push_back(s);
    }
    cycle_ = cmd.integer("cycle_100ns", 10000);
    if (cycle_ <= 0) return fail_reply("cycle_100ns must be > 0");
    max_delay_ = std::max<int64_t>(0, cmd.integer("max_delay_100ns", 0));
    duration_s_ = std::max(0.0, cmd.num("duration_s", 0));
    post_trigger_s_ = std::max(0.0, cmd.num("post_trigger_s", 0));
    pre_trigger_ms_ = std::max(0.0, cmd.num("pre_trigger_ms", 0));
    if (pre_trigger_ms_ > 600000) return fail_reply("pre_trigger_ms above 600000");
    int64_t me = cmd.integer("max_events", 0);
    max_events_ = me > 0 ? static_cast<uint64_t>(me) : 0;
    std::string err;
    if (const JVal* c = cmd.get("start_when"))
        if (!start_when_.parse(*c, specs.size(), specs, err)) return fail_reply("start_when: " + err);
    if (const JVal* c = cmd.get("stop_when"))
        if (!stop_when_.parse(*c, specs.size(), specs, err)) return fail_reply("stop_when: " + err);
    if (post_trigger_s_ > 0 && !start_when_.set) return fail_reply("post_trigger_s needs start_when");
    file_ = cmd.str("file");
    if (file_.empty()) return fail_reply("file required");
    std::string header = cmd.str("header");
    if (header.empty() || header[0] != '{' || header.find('\n') != std::string::npos)
        return fail_reply("header must be one JSON object line");
    std::string key_path = cmd.str("hmac_key_path");
    if (!key_path.empty() && !read_key(key_path, key_, err)) return fail_reply(err);
    writer_stall_ms_ = cmd.integer("debug_writer_stall_ms", 0);
    silent_ns_ = std::max<int64_t>(500000000LL, 50 * cycle_ * 100 + 3 * max_delay_ * 100);
    if (cmd.has("silent_ms")) silent_ns_ = std::max<int64_t>(1, cmd.integer("silent_ms", 500)) * 1000000LL;
    resub_after_ns_ = std::max<int64_t>(silent_ns_, cmd.integer("resubscribe_after_ms", 3000) * 1000000LL);
    resub_retry_ms_ = std::max<int64_t>(50, cmd.integer("resubscribe_retry_ms", 2000));
    check_ms_ = std::max<int64_t>(1, cmd.integer("version_check_ms", 20));
    const int64_t want_ver = cmd.integer("symbol_version", -1);   // the version the addresses were resolved with
    if (const JVal* st = cmd.get("stall")) {
        int64_t bid = st->integer("block", -1);
        for (size_t k = 0; k < specs.size(); ++k)
            if (static_cast<int64_t>(specs[k].id) == bid) stall_block_ = static_cast<int>(k);
        stall_off_ = static_cast<uint32_t>(st->integer("off", 0));
        stall_size_ = static_cast<uint32_t>(st->integer("size", 4));
        stall_after_ = st->integer("after_100ns", 3 * cycle_);
        if (stall_block_ < 0 || stall_size_ == 0 || stall_off_ + stall_size_ > specs[stall_block_].size)
            return fail_reply("stall: counter slot outside its block");
        stall_prev_.assign(stall_size_, 0);
    }

    // ------------------------------------------------ rings (pre-allocated before anything subscribes)
    double ring_s = cmd.num("ring_s", 10.0);
    double ring_mb = cmd.num("ring_mb", 0.0);
    double cap_mb = cmd.num("ring_cap_mb", 1024.0);
    std::vector<size_t> slots(specs.size());
    double total = 0;
    for (size_t k = 0; k < specs.size(); ++k) {
        slots[k] = static_cast<size_t>(std::ceil(ring_s * 1e7 / double(cycle_))) + 64;
        total += double(slots[k]) * (16.0 + ((specs[k].size + 7) & ~7u));
    }
    double budget = ring_mb > 0 ? ring_mb * 1048576.0 : std::min(total, cap_mb * 1048576.0);
    if (total > budget) {
        double f = budget / total;
        for (auto& s : slots) s = std::max<size_t>(4, static_cast<size_t>(double(s) * f));
    }
    ring_bytes_ = 0;
    for (size_t k = 0; k < specs.size(); ++k) {
        auto st = std::make_unique<BState>();
        st->spec = specs[k];
        try {
            st->ring.init(slots[k], specs[k].size);
        } catch (const std::bad_alloc&) {
            return fail_reply("not enough memory for the sample rings (lower ring_s / ring_mb)");
        }
        ring_bytes_ += st->ring.capacity() * st->ring.stride();
        blocks_.push_back(std::move(st));
    }
    ring_seconds_ = 1e300;
    for (auto& b : blocks_) ring_seconds_ = std::min(ring_seconds_, double(b->ring.capacity()) * double(cycle_) / 1e7);

    // ------------------------------------------------ file must not exist (never overwrite a recording)
    if (std::FILE* f = std::fopen(file_.c_str(), "rb")) {
        std::fclose(f);
        return fail_reply("file exists: " + file_);
    }

    // ------------------------------------------------ source + all-or-nothing subscription
    std::string backend = cmd.str("backend", "ads");
    if (backend == "sim") source_ = make_sim_source(cmd, err);
    else if (backend == "ads") source_ = make_ads_source(cmd, err);
    else return fail_reply("backend must be ads | sim");
    if (!source_) return fail_reply(err.empty() ? "source failed" : err);
    source_desc_ = source_->describe();
    // R43: the version the recording runs with, read before anything is subscribed (a change after this read is seen
    // by the first check); the addresses must come from the same program
    {
        uint32_t v = 0;
        std::string verr;
        if (source_->read_version(v, verr)) {
            if (want_ver >= 0 && static_cast<int64_t>(v) != want_ver) {
                source_.reset();
                JObj o;
                o.str("reply", "start").b("ok", false)
                    .str("error", "the PLC program changed after the variables were looked up (symbol version " +
                                  std::to_string(want_ver) + ", now " + std::to_string(v) + "): their addresses may "
                                  "belong to other variables — nothing subscribed, nothing written")
                    .i("symbol_version_now", v);
                return o.done();
            }
            watch_ = true;
            ver_ = v;
            ++checks_;
        } else {
            watch_error_ = verr;
        }
    }
    identity_ = source_->identity();
    auto now = std::chrono::steady_clock::now();
    t_start_ = now;
    for (auto& b : blocks_) b->progress = now;
    accepting_.store(true, std::memory_order_release);
    SubscribeError se;
    if (!source_->subscribe(specs, cycle_, max_delay_, this, se)) {
        accepting_.store(false);
        source_.reset();
        int bid = se.block >= 0 && se.block < int(specs.size()) ? int(specs[se.block].id) : -1;
        std::string what = bid >= 0 ? "block " + std::to_string(bid) + " (igrp 0x" : "subscription";
        if (bid >= 0) {
            char h[48];
            std::snprintf(h, sizeof h, "%X, ioffs 0x%X, %u bytes)", specs[se.block].igrp, specs[se.block].ioffs,
                          specs[se.block].size);
            what += h;
        }
        return fail_reply(what + ": " + se.message + " — nothing subscribed, nothing written", bid, se.code);
    }
    fh_ = std::fopen(file_.c_str(), "wb");
    if (!fh_) {
        unsubscribe_once();
        return fail_reply("cannot create " + file_);
    }
    std::setvbuf(fh_, nullptr, _IOFBF, 1 << 20);
    {
        std::lock_guard<std::mutex> lk(mu_);
        write_line(header + "\n");
    }
    armed_ = start_when_.set;
    started_ = true;
    writer_ = std::thread([this] { writer_loop(); });

    JObj ring;
    ring.d("seconds", ring_seconds_, 6).u("bytes", ring_bytes_);
    JObj o;
    o.str("reply", "start").b("ok", true).str("engine", "native").str("version", version_)
        .u("subscribed", specs.size()).raw("source", source_desc_).raw("ring", ring.done()).str("file", file_);
    return o.done();
}

void Engine::write_line(const std::string& line) {
    std::fwrite(line.data(), 1, line.size(), fh_);
    sha_.update(line);
    ++lines_;
    bytes_ += line.size();
}

void Engine::write_t0(int64_t ft) {
    have_t0_ = true;
    t0_ = ft;
    std::string l = "{\"_t0\": {\"filetime\": " + std::to_string(ft) + ", \"utc\": " + jq(utc_iso_from_filetime(ft)) +
                    ", \"source\": \"plc\"}}\n";
    write_line(l);
}

int64_t Engine::stamp(BState& s, int64_t ft) {
    int64_t missing = 0;
    if (s.have) {
        int64_t step = ft - s.last_ft;
        if (step <= 0) {
            ++s.dups;
        } else {
            if (!s.min_step || step < s.min_step) s.min_step = step;
            if (s.min_step && 2 * step > 3 * s.min_step) {
                missing = std::llround(double(step) / double(s.min_step)) - 1;
                if (s.gaps.size() < 20) s.gaps.emplace_back(s.last_ft, missing);
            }
        }
    } else {
        s.first_ft = ft;
        s.have = true;
    }
    s.last_ft = ft;
    ++s.samples;
    return missing;
}

// R5: every hole in the PLC timeline is written into the file where it happened, not only counted. Same line format
// as the Python recorder: {"_gap": {"block": id, "kind": "gap"|"head"|"tail", "t_from": ms, "t_to": ms, "missing": n}}
void Engine::write_gap(const BState& s, const char* kind, const std::string& from, const std::string& to, int64_t n) {
    std::string l = "{\"_gap\": {\"block\": " + std::to_string(s.spec.id) + ", \"kind\": \"" + kind + "\", \"t_from\": " +
                    from + ", \"t_to\": " + to + ", \"missing\": " + std::to_string(n) + "}}\n";
    write_line(l);
    ++gap_markers_;
}

// D11: a PLC in STOP keeps sending its notifications every cycle with every value frozen — including the task's
// CycleCount that every cycle recording carries. Samples whose counter did not advance for longer than stall_after_
// are not measurements of a running program: the interval is written into the file as a {"_stall": …} marker,
// the samples are counted as stalled, the label is INCOMPLETE. The bytes themselves stay as the PLC sent them.
void Engine::note_counter(size_t, int64_t ft, const uint8_t* d, uint32_t) {
    const uint8_t* c = d + stall_off_;
    if (!stall_have_prev_) {
        std::memcpy(stall_prev_.data(), c, stall_size_);
        stall_have_prev_ = true;
        stall_adv_ft_ = ft;
        return;
    }
    if (std::memcmp(stall_prev_.data(), c, stall_size_) != 0) {
        if (stall_open_) close_stall(ft);
        std::memcpy(stall_prev_.data(), c, stall_size_);
        stall_adv_ft_ = ft;
        stall_rep_ = 0;
        return;
    }
    ++stall_rep_;
    if (!stall_open_ && ft - stall_adv_ft_ > stall_after_) stall_open_ = true;
}

void Engine::close_stall(int64_t to_ft) {
    std::string l = "{\"_stall\": {\"block\": " + std::to_string(blocks_[stall_block_]->spec.id) + ", \"t_from\": " +
                    ms(stall_adv_ft_) + ", \"t_to\": " + ms(to_ft) + ", \"samples\": " + std::to_string(stall_rep_) + "}}\n";
    write_line(l);
    ++stalls_;
    stalled_samples_ += stall_rep_;
    stall_100ns_ += to_ft - stall_adv_ft_;
    if (stall_list_.size() < 20) stall_list_.emplace_back(stall_adv_ft_, to_ft);
    stall_open_ = false;
    stall_rep_ = 0;
}

// D14: while a block is silent the file cannot tell how much is missing yet (no PLC timestamp arrives); this PC's
// clock can: the silence so far, in sample periods. Reported live as an estimate, never written to the file.
double Engine::live_missing_estimate(std::chrono::steady_clock::time_point now) const {
    double est = 0;
    for (auto& bp : blocks_) {
        const BState& s = *bp;
        if (!s.silent || !s.have) continue;
        const int64_t step = step_of(s);
        if (step <= 0) continue;
        const double silent_100ns = std::chrono::duration<double, std::nano>(now - s.progress).count() / 100.0;
        est += std::max(0.0, silent_100ns / double(step) - 1.0);
    }
    return est;
}

// R9: a checkpoint commits to every byte before it (running SHA-256, HMAC-signed), so a file whose footer never got
// written (crash, power loss) is still provably intact up to its last checkpoint.
void Engine::write_chain() {
    if (lines_ == chain_lines_) return;            // nothing new since the last checkpoint
    const std::string d = sha_.hexdigest();
    std::string l = "{\"_chain\": {\"lines\": " + std::to_string(lines_) + ", \"bytes\": " + std::to_string(bytes_) +
                    ", \"sha256\": \"" + d + "\"";
    if (!key_.empty()) l += ", \"hmac\": \"" + hmac_sha256_hex(key_, d) + "\"";
    l += "}}\n";
    write_line(l);
    chain_lines_ = lines_;
    ++chains_;
}

// R43, engine mutex held: end the recording at ft — every block is waited for up to it (as N56); without a window to
// keep (still armed, nothing verified) at once.
void Engine::end_at(int64_t ft, bool have_ft, const std::string& r) {
    if (finish_req_.load()) return;
    if (armed_ || !have_ft) {
        finish_now(r, 0, false);
        return;
    }
    if (!have_cut_ || ft < cut_) {
        cut_ = ft;
        have_cut_ = true;
        cut_reason_ = r;
        stop_reason_ = r;
    }
    if (!stop_cut_pending_.load()) {
        stop_cut_wall_ = std::chrono::steady_clock::now();
        stop_cut_pending_.store(true);
    }
}

// R43, writer thread, engine mutex NOT held (the read is a request to the runtime).
void Engine::check_program(int64_t newest, bool flowing, std::chrono::steady_clock::time_point now) {
    uint32_t v = 0;
    std::string err;
    last_check_ = now;
    bool ok = source_->read_version(v, err);
    // the version byte alone proves nothing across a restart (another build can come back with the same byte): after
    // failed reads or a re-registration, and whenever it moved, the program's identity decides
    const bool after_gap = check_failing_ || resub_since_check_;
    const bool use_id = ok && !identity_.empty() && (v != ver_ || after_gap);
    std::string id_now;
    if (use_id) {
        id_now = source_->identity();
        if (id_now.empty()) {
            ok = false;
            err = "the program identity (compile time, online changes, table sizes) could not be read";
        }
    }
    std::lock_guard<std::mutex> lk(mu_);
    ++checks_;
    if (!ok) {
        ++check_failures_;
        check_error_ = err;
        if (!check_failing_) {
            check_failing_ = true;
            check_fail_since_ = now;
            check_fail_newest_ = newest;
        }
        // samples keep coming while the version cannot be read: they cannot be verified — end at the last verified one
        // (an outage delivers nothing: waited for; the identity decides when the runtime is back)
        const auto bound = std::chrono::seconds(1) + std::chrono::nanoseconds(max_delay_ * 100);
        if (!watch_lost_ && flowing && now - check_fail_since_ > bound && newest > check_fail_newest_ &&
            !finish_req_.load()) {
            watch_lost_ = true;
            char r[320];
            std::snprintf(r, sizeof r, "the PLC program version could not be read for %.1f s while samples kept coming "
                          "(%s): a download / online change could not be ruled out — the recording ends at the last "
                          "verified sample",
                          std::chrono::duration<double>(now - check_fail_since_).count(), err.c_str());
            end_at(verified_ft_, have_verified_, r);
        }
        return;
    }
    check_failing_ = false;
    const bool same_program = use_id ? id_now == identity_ : v == ver_;
    if (same_program) {
        if (v != ver_) {
            // the version byte moved, the program did not (a TwinCAT restart, the same build downloaded again)
            program_notes_.push_back("symbol version " + std::to_string(ver_) + " -> " + std::to_string(v) +
                                     " with the same program (" + id_now + "): the recording continued");
            ver_ = v;
        }
        resub_since_check_ = false;
        if (!have_verified_ || newest > verified_ft_) verified_ft_ = newest;
        have_verified_ = true;
        return;
    }
    if (program_changed_) return;
    program_changed_ = true;
    changed_from_ = ver_;
    changed_to_ = v;
    changed_seen_ft_ = newest;
    std::string what = "symbol version " + std::to_string(ver_) + " -> " + std::to_string(v);
    if (use_id) what += ", program " + identity_ + " -> " + id_now;
    end_at(verified_ft_, have_verified_,
           "the PLC program changed (" + what + ": download / online change / another build) - the notifications "
           "sample memory addresses that may now hold other variables; the recording ends at the last sample verified "
           "before the change");
}

static bool identity_differs(const std::string& a, const std::string& b) {
    // "t<compile time>": unknown on either side ("") means: cannot tell — not different
    return !a.empty() && !b.empty() && a != b;
}

void Engine::finish_now(const std::string& reason, int64_t end_ft, bool have_end) {
    if (finish_req_.load()) return;
    if (stop_reason_.empty()) stop_reason_ = reason;
    if (have_end) {
        have_stop_ft_ = true;
        stop_ft_ = end_ft;
    }
    finish_req_.store(true);
}

void Engine::commit(size_t b, int64_t ft, const uint8_t* d, uint32_t n) {
    BState& s = *blocks_[b];
    uint32_t len = n;
    if (n != s.spec.size) {
        ++s.size_mismatch;
        if (len > s.spec.size) len = s.spec.size;
    }
    if (any_emitted_ && ft < last_emitted_) ++s.late;
    const bool had = s.have;
    const int64_t prev_ft = s.last_ft;
    const int64_t gap = stamp(s, ft);
    if (had && gap > 0) write_gap(s, "gap", ms(prev_ft), ms(ft), gap);
    row_.clear();
    row_ += '[';
    row_ += ms(ft);
    row_ += ",\"#";
    row_ += std::to_string(s.spec.id);
    row_ += "\",\"";
    base64_append(row_, d, len);
    row_ += "\"]\n";
    write_line(row_);
    ++samples_;
    events_ += s.spec.leaves;
    if (!any_emitted_ || ft > last_emitted_) last_emitted_ = ft;
    any_emitted_ = true;
    if (static_cast<int>(b) == stall_block_ && len >= stall_off_ + stall_size_) note_counter(b, ft, d, n);
    if (stop_when_.set && stop_when_.block == b && stop_when_.fires(d, n)) {
        finish_now("stop_when " + stop_when_.text, ft, true);
        return;
    }
    if (max_events_ && events_ >= max_events_) finish_now("max_events " + std::to_string(max_events_), ft, true);
}

void Engine::process(size_t b, int64_t ft, const uint8_t* d, uint32_t n) {
    if (!have_t0_) write_t0(ft);
    if (have_cut_ && ft > cut_) {
        ++after_limit_;
        finish_now(cut_reason_, cut_, true);
        return;
    }
    if (armed_) {
        BState& s = *blocks_[b];
        uint32_t len = std::min<uint32_t>(n, s.spec.size);
        pre_.push_back(Pre{static_cast<uint32_t>(b), ft, n, std::vector<uint8_t>(d, d + len)});
        const int64_t pre100 = std::llround(pre_trigger_ms_ * 1e4);
        // R41: blocks are separate notifications and arrive with a skew — block 1's sample of cycle t+1 can come
        // before block 0's (trigger) sample of cycle t. Purging against the newest stamp of ANY block dropped block
        // 1's sample of the trigger cycle (missing_head 1 on a lossless capture). Keep the delivery skew as margin;
        // only samples inside the window are committed below.
        const int64_t margin = max_delay_ + 500000;   // + 50 ms
        while (!pre_.empty() && pre_.front().ft < ft - pre100 - margin) pre_.pop_front();
        if (armed_seen_.size() != blocks_.size()) armed_seen_.assign(blocks_.size(), 0);
        if (!armed_seen_[b]) {
            armed_seen_[b] = 1;
            ++armed_seen_n_;
        }
        if (!have_armed_t0_) {
            armed_t0_ = ft;
            have_armed_t0_ = true;
        }
        const bool all_live = armed_seen_n_ >= blocks_.size() || ft - armed_t0_ > 2000000;
        bool fire = false;
        if (start_when_.block == b) {
            if (start_when_.fires(d, n)) {
                if (start_when_.edge || all_live) {
                    fire = true;
                } else if (!have_warm_true_) {
                    have_warm_true_ = true;        // N61: true before every block delivered — remembered, reported
                    warm_true_ft_ = ft;
                }
            } else if (have_warm_true_) {
                // false after the warm-up truth (also still within the warm-up): that truth is over. A later fire is a
                // new truth, not the warm-up's delayed (reporting it as "delayed by" the whole quiet time was false)
                warm_lapsed_ = true;
            }
        }
        if (fire) {
            armed_ = false;
            triggered_ = true;
            trig_ft_ = ft;
            ws_ = ft - pre100;
            have_ws_ = true;
            ws_from_trigger_ = true;
            if (post_trigger_s_ > 0) {
                int64_t c = ft + std::llround(post_trigger_s_ * 1e7);
                if (!have_cut_ || c < cut_) {
                    cut_ = c;
                    have_cut_ = true;
                    char r[96];
                    std::snprintf(r, sizeof r, "post_trigger %gs after the start_when trigger", post_trigger_s_);
                    cut_reason_ = r;
                }
            }
            std::deque<Pre> pre;
            pre.swap(pre_);
            for (auto& p : pre) {
                if (finish_req_.load()) break;
                if (p.ft < ws_) continue;              // the skew margin, not the window
                commit(p.b, p.ft, p.d.data(), p.n);   // p.d holds min(n, block size) bytes
            }
        }
        return;
    }
    // a block's sample from before the window, delivered after the trigger (skew): outside the recording
    if (ws_from_trigger_ && have_ws_ && ft < ws_) return;
    if (!ws_from_trigger_ && have_ws_ && ft < ws_) {
        // N84: the window starts when every block has delivered; a block that started earlier delivered rows before
        // it. They stay in the file as delivered, counted apart (rows_before_window) and not in any block's count —
        // the readers take the footer's window as the recording
        row_.clear();
        row_ += '[';
        row_ += ms(ft);
        row_ += ",\"#";
        row_ += std::to_string(blocks_[b]->spec.id);
        row_ += "\",\"";
        base64_append(row_, d, std::min<uint32_t>(n, blocks_[b]->spec.size));
        row_ += "\"]\n";
        write_line(row_);
        ++rows_before_window_;
        return;
    }
    commit(b, ft, d, n);
}

void Engine::unsubscribe_once() {
    std::call_once(unsub_once_, [this] {
        if (source_) source_->unsubscribe();
        accepting_.store(false, std::memory_order_release);
    });
}

void Engine::writer_loop() {
    using clock = std::chrono::steady_clock;
    if (writer_stall_ms_ > 0) std::this_thread::sleep_for(std::chrono::milliseconds(writer_stall_ms_));
    last_flush_ = clock::now();
    const auto silent = std::chrono::nanoseconds(silent_ns_);
    const int64_t i64max = std::numeric_limits<int64_t>::max();
    for (;;) {
        const bool stopping = stop_req_.load();
        const auto now = clock::now();
        bool all_silent = true, wait_for_block = false;
        size_t backlog = 0;
        int64_t W = i64max;
        int64_t newest = 0;                // the newest stamp delivered by any block (R43: what a check can verify)
        bool have_newest = false;
        for (auto& bp : blocks_) {
            BState& s = *bp;
            const uint64_t h = s.ring.head();
            if (h != s.seen_head) {
                s.seen_head = h;
                s.progress = now;
                s.silent = false;
            } else if (!s.silent && now - s.progress > silent) {
                s.silent = true;
                s.ever_silent = true;
            }
            if (!s.silent) all_silent = false;
            const size_t sz = static_cast<size_t>(h - s.ring.tail());
            backlog += sz;
            if (sz > 0 || s.consumed_any) {
                const int64_t f = sz > 0 ? s.ring.ft_at(h - 1) : s.consumed_ft;
                if (!have_newest || f > newest) newest = f;
                have_newest = true;
            }
            if (!s.silent) {
                if (sz > 0) W = std::min(W, s.ring.ft_at(h - 1));
                else if (s.consumed_any) W = std::min(W, s.consumed_ft);
                else wait_for_block = true;    // not delivered yet: wait (up to the silence timeout)
            }
        }
        if (stopping) {
            W = i64max;
            wait_for_block = false;
        }
        if (stop_cut_pending_.load() &&
            now - stop_cut_wall_ > std::chrono::seconds(1) + std::chrono::nanoseconds(max_delay_ * 100)) {
            W = cut_;                      // N56: the wait is over: write what is up to the cut, block by block
            wait_for_block = false;
        }
        // R43: only samples a version read has verified are written
        if (watch_ && source_ && !finish_req_.load() && have_newest && (!have_verified_ || newest > verified_ft_)) {
            const int64_t period = check_failing_ ? std::max<int64_t>(check_ms_, 500) : check_ms_;
            const bool final_check = stopping && !final_check_done_;
            if (final_check || now - last_check_ >= std::chrono::milliseconds(period))
                check_program(newest, !all_silent, now);
        }
        if (stopping) final_check_done_ = true;
        const int64_t Wv = !watch_ ? i64max : have_verified_ ? verified_ft_ : std::numeric_limits<int64_t>::min();
        W = std::min(W, Wv);
        size_t done = 0;
        bool drained = false;              // nothing left that could still be written (R43: unverified rows aside)
        {
            std::lock_guard<std::mutex> lk(mu_);
            if (backlog > max_backlog_) max_backlog_ = backlog;
            if (!wait_for_block) {
                while (done < 8192 && !finish_req_.load(std::memory_order_relaxed)) {
                    int best = -1;
                    int64_t bft = 0;
                    for (size_t k = 0; k < blocks_.size(); ++k) {
                        SampleRing& r = blocks_[k]->ring;
                        if (r.head() == r.tail()) continue;
                        int64_t ft = r.ft_at(r.tail());
                        if (best < 0 || ft < bft) {
                            best = static_cast<int>(k);
                            bft = ft;
                        }
                    }
                    if (best < 0 || bft > W) break;
                    if (!have_origin_) {
                        // window origin: the moment every delivering block has its first sample
                        int64_t o = bft;
                        for (auto& bp : blocks_) {
                            SampleRing& r = bp->ring;
                            if (!bp->consumed_any && r.head() != r.tail()) o = std::max(o, r.ft_at(r.tail()));
                        }
                        have_origin_ = true;
                        origin_ = o;
                        if (!start_when_.set) {
                            ws_ = o;
                            have_ws_ = true;
                        }
                        if (duration_s_ > 0 && (!have_cut_ || o + std::llround(duration_s_ * 1e7) < cut_)) {
                            cut_ = o + std::llround(duration_s_ * 1e7);   // (an earlier end set before stands: R43)
                            have_cut_ = true;
                            char r[64];
                            std::snprintf(r, sizeof r, "duration %gs", duration_s_);
                            cut_reason_ = r;
                        }
                        t_first_ = now;
                        have_first_wall_ = true;
                    }
                    BState& s = *blocks_[best];
                    const uint64_t t = s.ring.tail();
                    process(static_cast<size_t>(best), bft, s.ring.data_at(t), s.ring.n_at(t));
                    s.consumed_any = true;
                    s.consumed_ft = bft;
                    s.ring.pop();
                    ++done;
                }
            }
            {
                size_t left_rows = 0;
                bool only_unverified = watch_;
                for (auto& bp : blocks_) {
                    SampleRing& r = bp->ring;
                    const size_t n = static_cast<size_t>(r.head() - r.tail());
                    if (n == 0) continue;
                    left_rows += n;
                    if (r.ft_at(r.tail()) <= Wv) only_unverified = false;
                }
                // rows only a version read could release, and the reads fail (the runtime is away / gone): they are
                // not waited for — counted at the end (unverified_rows)
                drained = left_rows == 0 || (only_unverified && (check_failing_ || final_check_done_));
            }
            // D13: every block silent for resub_after → the runtime restarted (CONFIG→RUN) or the route dropped; the
            // handles we hold are dead. Ask the source for new ones, at most every 2 s, until it succeeds.
            if (!finish_req_.load() && !stopping && all_silent && have_first_wall_ && source_) {
                bool long_silent = true;
                for (auto& bp : blocks_)
                    if (now - bp->progress < std::chrono::nanoseconds(resub_after_ns_)) long_silent = false;
                if (long_silent && (!resub_tried_ || now - last_resub_try_ > std::chrono::milliseconds(resub_retry_ms_))) {
                    resub_tried_ = true;
                    last_resub_try_ = now;
                    std::string err;
                    const std::string id_now = source_->identity();
                    if (identity_differs(identity_, id_now)) {
                        // R43: another build / an online change came back — its variables need not be where the old
                        // ones were
                        rebuild_note_ = "the PLC program changed while TwinCAT was away (program " + identity_ +
                                        " -> " + id_now + ": compile time, online changes, table sizes): the "
                                        "notifications were not registered again "
                                        "at the old program's addresses; the recording ends at the last " +
                                        std::string(watch_ ? "verified sample" : "sample") + " before the outage";
                        if (watch_) end_at(verified_ft_, have_verified_, rebuild_note_);
                        else end_at(last_emitted_, any_emitted_, rebuild_note_);
                    } else if (source_->resubscribe(err)) {
                        resub_since_check_ = true;   // the next read compares the program identity too
                        char n[200];
                        std::snprintf(n, sizeof n, "notifications registered again after %.1f s without samples (TwinCAT restart / route loss?) at %s ms",
                                      std::chrono::duration<double>(now - blocks_[0]->progress).count(),
                                      have_t0_ ? ms(last_emitted_).c_str() : "?");
                        resub_notes_.push_back(n);
                        resub_error_.clear();
                        for (auto& bp : blocks_) bp->progress = now;   // give the new handles their silence budget
                    } else {
                        ++resub_failures_;
                        resub_error_ = err;
                    }
                }
            }
            // limits on this PC's clock (the PLC stopped delivering: nothing would ever reach the cut)
            if (!finish_req_.load() && !stopping && duration_s_ > 0) {
                const auto grace = std::chrono::seconds(1) + std::chrono::nanoseconds(max_delay_ * 100);
                const auto dur = std::chrono::nanoseconds(static_cast<int64_t>(duration_s_ * 1e9));
                if (!have_first_wall_ && drained && now - t_start_ > dur + std::chrono::seconds(5)) {
                    finish_now(backlog == 0 ? "duration elapsed with no data (PLC not running? symbols never updated?)"
                                            : "duration elapsed: no sample could be verified against a program change "
                                              "(the symbol version could not be read again)", 0, false);
                } else if (have_first_wall_ && all_silent && drained && now - t_first_ > dur + grace && have_cut_) {
                    char r[160];
                    std::snprintf(r, sizeof r, "duration %gs: the PLC stopped delivering at %s ms (rest of the window counted as missing_tail)",
                                  duration_s_, have_t0_ ? ms(last_emitted_).c_str() : "?");
                    finish_now(r, cut_, true);
                }
            }
            // N56: an end at a PLC time (stop on request, R43 program change) — every block delivers up to it
            // (process() ends the recording at the first sample after it; a block that delivered past it while its
            // rows wait for nothing more ends it too), or the wait (max_delay + 1 s) ends: then what is up to the cut is
            // written and the rest of a block that did not get there is its tail (missing, allowance 1)
            if (!finish_req_.load() && stop_cut_pending_.load()) {
                const bool waited =
                    now - stop_cut_wall_ > std::chrono::seconds(1) + std::chrono::nanoseconds(max_delay_ * 100);
                bool left = false, behind = false;
                for (auto& bp : blocks_) {
                    SampleRing& r = bp->ring;
                    const uint64_t h = r.head();
                    if (h != r.tail()) {
                        const int64_t t = r.ft_at(r.tail());
                        // a row up to the cut is left while it can still be written: verified, or verifiable (reads ok)
                        if (t <= cut_ && (t <= Wv || !check_failing_)) left = true;
                    }
                    if (!bp->silent) {
                        const bool hn = h != r.tail() || bp->consumed_any;
                        const int64_t nw = h != r.tail() ? r.ft_at(h - 1) : bp->consumed_ft;
                        if (!hn || nw <= cut_) behind = true;
                    }
                }
                if (!left && (waited || !behind)) finish_now(cut_reason_, cut_, true);
            }
            if (fh_ && now - last_flush_ > std::chrono::seconds(1)) {
                if (have_t0_) write_chain();
                std::fflush(fh_);
                last_flush_ = now;
            }
        }
        if (finish_req_.load()) break;
        if (stopping && drained && done == 0) break;
        if (done == 0) std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
    unsubscribe_once();
    {
        std::lock_guard<std::mutex> lk(mu_);
        finish();
    }
    finished_.store(true);
    if (emit) emit("{\"event\": \"finished\", \"footer\": " + footer_ + "}");
}

int64_t Engine::step_of(const BState& s) const {
    if (s.min_step > 0) return s.min_step;
    int64_t best = 0;
    for (auto& bp : blocks_)
        if (bp->min_step > 0 && (!best || bp->min_step < best)) best = bp->min_step;
    return best ? best : cycle_;
}

int64_t Engine::head_missing(const BState& s) const {
    if (!ws_from_trigger_ || !have_ws_ || !s.have || s.first_ft <= ws_) return 0;
    return (s.first_ft - ws_) / step_of(s);
}

int64_t Engine::inner_missing(const BState& s) const {
    if (s.samples < 2 || s.min_step <= 0) return 0;
    int64_t expected = std::llround(double(s.last_ft - s.first_ft) / double(s.min_step)) + 1;
    return std::max<int64_t>(0, expected - static_cast<int64_t>(s.samples));
}

int64_t Engine::tail_missing(const BState& s, int64_t end, bool have_end) const {
    if (!have_end) return 0;
    if (!s.have) {
        if (!have_ws_ || end < ws_) return 0;
        return (end - ws_) / step_of(s) + 1;   // never delivered: the whole window is missing
    }
    if (end <= s.last_ft) return 0;
    // one period of allowance, the same rule as the Python recorder and rec_verify. A stop on request ends at a PLC
    // time every block is waited for (N56), so no other allowance exists: a block that ends early has lost samples.
    return std::max<int64_t>(0, (end - s.last_ft) / step_of(s) - 1);
}

int64_t Engine::window_end(bool& have) const {
    if (have_stop_ft_) {
        have = true;
        return stop_ft_;
    }
    int64_t e = 0;
    have = false;
    for (auto& bp : blocks_)
        if (bp->have && (!have || bp->last_ft > e)) {
            e = bp->last_ft;
            have = true;
        }
    return e;
}

std::string Engine::block_json(const BState& s, int64_t end, bool have_end) const {
    JObj o;
    o.u("id", s.spec.id).u("samples", s.samples).i("missing_head", head_missing(s))
        .i("missing_gaps", inner_missing(s)).i("missing_tail", tail_missing(s, end, have_end))
        .u("overflow", s.ring.overflow());
    o.raw("period_ms", s.min_step > 0 ? jnum(double(s.min_step) / 1e4, 10) : "null");
    o.u("duplicate_stamps", s.dups);
    if (s.late) o.u("late", s.late);
    if (s.size_mismatch) o.u("size_mismatch", s.size_mismatch);
    if (s.ever_silent) o.b("was_silent", true);
    if (s.silent && !finished_.load()) o.b("silent", true);
    std::vector<std::string> g;
    for (size_t k = 0; k < s.gaps.size() && k < 5; ++k)
        g.push_back("[" + (have_t0_ ? ms(s.gaps[k].first) : std::string("null")) + ", " + std::to_string(s.gaps[k].second) + "]");
    o.raw("first_gaps", jarr(g));
    return o.done();
}

void Engine::finish() {
    bool have_end = false;
    int64_t end = window_end(have_end);
    // what the rings still hold: past the end (after_limit), or inside it but never verified against a program change
    // (R43: delivered, not written — counted as missing where they fall)
    for (auto& bp : blocks_) {
        SampleRing& r = bp->ring;
        for (uint64_t i = r.tail(); i != r.head(); ++i) {
            const int64_t ft = r.ft_at(i);
            if (watch_ && (!have_verified_ || ft > verified_ft_) && (!have_end || ft <= end)) ++unverified_rows_;
            else ++after_limit_;
        }
    }
    int64_t head = 0, gaps = 0, tail = 0;
    uint64_t ovf = 0, mismatch = 0;
    std::vector<std::string> bl, ivs;
    for (auto& bp : blocks_) {
        const BState& s = *bp;
        head += head_missing(s);
        gaps += inner_missing(s);
        tail += tail_missing(s, end, have_end);
        ovf += s.ring.overflow();
        mismatch += s.size_mismatch;
        if (bl.size() < 50) bl.push_back(block_json(s, end, have_end));
        std::vector<OverflowInterval> list;
        for (size_t k = 0; k < s.ring.intervals_done(); ++k) list.push_back(s.ring.interval(k));
        OverflowInterval open;
        if (s.ring.open_interval(open)) list.push_back(open);
        for (auto& iv : list)
            if (ivs.size() < 16)
                ivs.push_back("[" + std::to_string(s.spec.id) + ", " + (have_t0_ ? ms(iv.first_ft) : "null") + ", " +
                              (have_t0_ ? ms(iv.last_ft) : "null") + ", " + std::to_string(iv.count) + "]");
    }
    for (auto& bp : blocks_)
        if (bp->size_mismatch)
            errors_.push_back("block " + std::to_string(bp->spec.id) + ": " + std::to_string(bp->size_mismatch) +
                              " samples with a size other than the block size");
    if (start_when_.set && !triggered_) errors_.push_back("start_when never fired: nothing recorded after the trigger");
    if (start_when_.set && !triggered_ && have_warm_true_ && have_t0_)
        errors_.push_back("start_when was true at " + ms(warm_true_ft_) + " ms during the warm-up (before every block had "
                          "delivered, at most 200 ms) and false afterwards — it did not fire");
    if (stop_on_request_)
        stop_wait_ms_ = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - stop_cut_wall_).count();
    if (fh_ && have_t0_ && stall_open_) close_stall(have_end ? std::max(end, stall_adv_ft_) : blocks_[stall_block_]->last_ft);
    for (auto& n : resub_notes_) errors_.push_back(n);
    if (!resub_error_.empty()) errors_.push_back("re-registering the notifications failed " + std::to_string(resub_failures_) +
                                                 " time(s), last: " + resub_error_);
    const int64_t miss = head + gaps + tail;
    const uint64_t good = samples_ > stalled_samples_ ? samples_ - stalled_samples_ : 0;
    const double comp = (samples_ + miss) ? double(good) / double(samples_ + static_cast<uint64_t>(miss)) : 0.0;
    const bool pass = samples_ > 0 && miss == 0 && ovf == 0 && mismatch == 0 && (!start_when_.set || triggered_) && stalls_ == 0;
    std::string label_reason;
    if (stalls_ > 0 && have_t0_) {
        label_reason = "PLC stopped (cycle counter frozen) ";
        for (size_t k = 0; k < stall_list_.size() && k < 5; ++k)
            label_reason += (k ? ", " : "") + ms(stall_list_[k].first) + "-" + ms(stall_list_[k].second) + " ms";
    }
    // head / tail holes as markers too (block order), then the footer's digest covers them
    if (fh_ && have_t0_) {
        for (auto& bp : blocks_) {
            const BState& s = *bp;
            const int64_t h = head_missing(s);
            if (h > 0) write_gap(s, "head", ms(ws_), ms(s.first_ft), h);
            const int64_t t = tail_missing(s, end, have_end);
            if (t > 0) write_gap(s, "tail", s.have ? ms(s.last_ft) : (have_ws_ ? ms(ws_) : std::string("null")), ms(end), t);
        }
    }
    std::vector<std::string> errs;
    for (auto& e : errors_) errs.push_back(jq(e));
    int64_t span_end = 0;
    bool have_span = false;
    for (auto& bp : blocks_)
        if (bp->have && (!have_span || bp->last_ft > span_end)) {
            span_end = bp->last_ft;
            have_span = true;
        }
    const std::string digest = sha_.hexdigest();
    JObj f;
    f.b("_footer", true).str("engine", "native").str("engine_version", version_)
        .u("events", events_).u("samples", samples_).i("missing_samples", miss)
        .i("missing_head", head).i("missing_gaps", gaps).i("missing_tail", tail)
        .u("overflow_samples", ovf).raw("overflow_intervals", jarr(ivs))
        .u("dropped", ovf).i("dropped_on_stop", 0).u("after_limit", after_limit_)
        .raw("unsubscribed", "[]");
    if (stop_reason_.empty()) f.null("stop_reason");
    else f.str("stop_reason", stop_reason_);
    if (duration_s_ > 0) f.d("requested_s", duration_s_);
    else f.null("requested_s");
    f.raw("plc_span_s", have_span && have_t0_ ? jnum(double(span_end - t0_) / 1e7, 12) : "null");
    f.b("manual_stop", manual_stop_.load() && !have_stop_ft_);
    f.raw("max_delay_ms", jnum(double(max_delay_) / 1e4, 6));
    f.raw("window_start_ms", have_ws_ && have_t0_ ? ms(ws_) : "null");
    f.raw("window_end_ms", have_end && have_t0_ ? ms(end) : "null");
    if (triggered_) f.raw("triggered_at_ms", ms(trig_ft_));
    if (triggered_ && have_warm_true_ && !warm_lapsed_) {
        f.raw("trigger_delayed_ms", jnum(double(trig_ft_ - warm_true_ft_) / 1e4, 6));
        f.str("trigger_note", "start_when was already true at " + ms(warm_true_ft_) + " ms, before every block had "
              "delivered; it fired on the first cycle all blocks delivered (the window cannot start before a block's "
              "first sample)");
    } else if (triggered_ && have_warm_true_ && have_t0_) {
        f.str("trigger_note", "start_when was true at " + ms(warm_true_ft_) + " ms during the warm-up (before every "
              "block had delivered) and false afterwards; it fired at " + ms(trig_ft_) + " ms, when it was true again");
    }
    if (start_when_.set && have_warm_true_ && have_t0_ && (!triggered_ || warm_lapsed_))
        f.raw("start_when_warmup_true_ms", ms(warm_true_ft_));
    {
        JObj pw;
        pw.b("checked", watch_);
        if (!watch_) {
            pw.str("error", watch_error_.empty() ? std::string("not readable") : watch_error_);
        } else {
            pw.i("check_ms", check_ms_).u("reads", checks_).u("failed_reads", check_failures_);
            if (!check_error_.empty()) pw.str("last_error", check_error_);
            pw.raw("verified_to_ms", have_verified_ && have_t0_ ? ms(verified_ft_) : "null");
        }
        if (!program_notes_.empty()) {
            std::vector<std::string> pn;
            for (auto& n : program_notes_) pn.push_back(jq(n));
            pw.raw("notes", jarr(pn));
        }
        f.raw("program_watch", pw.done());
    }
    if (!watch_)
        f.str("program_watch_error", "the symbol version could not be read at the start (" +
                                     (watch_error_.empty() ? std::string("not readable") : watch_error_) +
                                     "): a download / online change during the recording would not be noticed");
    if (program_changed_) {
        JObj pc;
        pc.raw("after_ms", have_verified_ && have_t0_ ? ms(verified_ft_) : "null")
            .raw("read_by_ms", have_t0_ ? ms(changed_seen_ft_) : "null")
            .raw("symbol_version", "[" + std::to_string(changed_from_) + "," + std::to_string(changed_to_) + "]")
            .str("rule", "the change happened after the last verified sample (after_ms) and before the version read "
                         "taken once the sample at read_by_ms had been delivered; samples between are not written");
        f.raw("program_changed", pc.done());
    }
    if (!rebuild_note_.empty()) f.str("program_rebuilt_in_outage", rebuild_note_);
    if (watch_lost_) f.b("program_watch_lost", true);
    if (rows_before_window_) f.u("rows_before_window", rows_before_window_);
    if (unverified_rows_) {
        f.u("unverified_rows", unverified_rows_);
        f.str("unverified_note", "samples delivered after the last verified one (" +
                                 (have_verified_ && have_t0_ ? ms(verified_ft_) + " ms" : std::string("none verified")) +
                                 ") but not written: the symbol version could not be read again (" + check_error_ +
                                 "), so a download / online change could not be ruled out — counted as missing where "
                                 "they fall");
    }
    if (stop_on_request_ && have_t0_) {
        JObj so;
        so.raw("at_ms", ms(cut_)).d("waited_ms", stop_wait_ms_, 3)
            .str("rule", "ends at the newest PLC time delivered when the stop was asked for; every block must deliver "
                         "up to it (allowance 1 period)");
        f.raw("stop_on_request", so.done());
    }
    f.d("completeness", comp, 8).str("verdict_hint", pass ? "PASS" : "FAIL").str("label", pass ? "COMPLETE" : "INCOMPLETE")
        .u("gap_markers", gap_markers_).u("chain_checkpoints", chains_)
        .u("stalls", stalls_).u("stalled_samples", stalled_samples_).d("stall_s", double(stall_100ns_) / 1e7, 8)
        .u("resubscriptions", source_ ? source_->resubscriptions() : 0);
    if (!label_reason.empty()) f.str("label_reason", label_reason);
    if (stall_block_ >= 0) f.u("stall_counter_block", blocks_[stall_block_]->spec.id);
    f.raw("blocks", jarr(bl)).raw("errors", jarr(errs));
    if (!key_error_.empty()) f.str("key_error", key_error_);
    f.str("stopped", utc_iso_now()).u("lines", lines_).str("sha256", digest);
    if (!key_.empty()) f.str("hmac", hmac_sha256_hex(key_, digest));
    footer_ = f.done();
    if (fh_) {
        std::string l = footer_ + "\n";
        std::fwrite(l.data(), 1, l.size(), fh_);
        std::fflush(fh_);
        std::fclose(fh_);
        fh_ = nullptr;
    }
}

std::string Engine::status() {
    std::lock_guard<std::mutex> lk(mu_);
    bool have_end = false;
    int64_t end = window_end(have_end);
    int64_t head = 0, gaps = 0, tail = 0;
    uint64_t ovf = 0;
    size_t backlog = 0, silent = 0;
    std::vector<std::string> bl;
    for (auto& bp : blocks_) {
        const BState& s = *bp;
        head += head_missing(s);
        gaps += inner_missing(s);
        tail += tail_missing(s, end, have_end);
        ovf += s.ring.overflow();
        backlog += s.ring.size();
        if (s.silent) ++silent;
        if (bl.size() < 20) bl.push_back(block_json(s, end, have_end));
    }
    const int64_t miss = head + gaps + tail;
    const auto now = std::chrono::steady_clock::now();
    const double elapsed = have_first_wall_ ? std::chrono::duration<double>(now - t_first_).count() : 0.0;
    const double est = finished_.load() ? 0.0 : live_missing_estimate(now);
    const uint64_t stalled_now = stalled_samples_ + (stall_open_ ? stall_rep_ : 0);
    const uint64_t good = samples_ > stalled_now ? samples_ - stalled_now : 0;
    JObj o;
    o.str("reply", "status").b("ok", true).str("engine", "native").b("active", started_ && !finished_.load())
        .b("armed", armed_).raw("triggered_at_ms", triggered_ && have_t0_ ? ms(trig_ft_) : "null")
        .u("samples", samples_).u("events", events_).i("missing_samples", miss).i("missing_head", head)
        .i("missing_gaps", gaps).i("missing_tail", tail).u("overflow_samples", ovf).u("backlog", backlog)
        .u("max_backlog", max_backlog_).u("ring_capacity_samples", blocks_.empty() ? 0 : blocks_[0]->ring.capacity())
        .d("ring_seconds", ring_seconds_, 6).u("ring_bytes", ring_bytes_).u("bytes_written", bytes_).u("lines", lines_)
        .raw("plc_span_s", any_emitted_ && have_t0_ ? jnum(double(last_emitted_ - t0_) / 1e7, 12) : "0")
        .d("elapsed_s", elapsed, 6).u("silent_blocks", silent).u("after_limit", after_limit_);
    o.d("completeness", (double(samples_) + double(miss) + est) > 0 ? double(good) / (double(samples_) + double(miss) + est) : 0.0, 8);
    if (est > 0) o.d("missing_silent_est", est, 8).b("completeness_estimated", true);
    o.u("stalls", stalls_).u("stalled_samples", stalled_now).b("stalled", stall_open_);
    if (stall_open_ && have_t0_) o.raw("stalled_since_ms", ms(stall_adv_ft_));
    o.u("resubscriptions", source_ ? source_->resubscriptions() : 0);
    if (!resub_error_.empty()) o.str("resubscribe_error", resub_error_);
    if (have_warm_true_ && have_t0_) {
        o.raw("start_when_warmup_true_ms", ms(warm_true_ft_));
        if (triggered_ && !warm_lapsed_) o.raw("trigger_delayed_ms", jnum(double(trig_ft_ - warm_true_ft_) / 1e4, 6));
    }
    if (stop_cut_pending_.load() && have_t0_) o.raw("stopping_at_ms", ms(cut_));
    if (!watch_) o.str("program_watch_error", watch_error_.empty() ? std::string("not readable") : watch_error_);
    else if (have_verified_ && have_t0_) o.raw("verified_to_ms", ms(verified_ft_));
    if (check_failing_) o.str("version_read_error", check_error_);
    if (program_changed_) o.b("program_changed", true);
    if (stop_reason_.empty()) o.null("stop_reason");
    else o.str("stop_reason", stop_reason_);
    std::vector<std::string> errs;
    for (auto& e : errors_) errs.push_back(jq(e));
    o.raw("errors", jarr(errs)).raw("blocks", jarr(bl)).raw("source", source_desc_.empty() ? "null" : source_desc_)
        .str("file", file_);
    if (finished_.load()) o.raw("footer", footer_);
    return o.done();
}

std::string Engine::stop(const std::string& reason) {
    if (!started_) {
        JObj o;
        o.str("reply", "stop").b("ok", false).str("error", "not started");
        return o.done();
    }
    if (!finished_.load()) {
        bool at_cut = false;
        {
            std::lock_guard<std::mutex> lk(mu_);
            if (stop_reason_.empty()) stop_reason_ = reason;
            // N56: end at the newest PLC time delivered so far; keep the notifications until every block reached it
            if (!armed_ && have_t0_ && !finish_req_.load() && !stop_cut_pending_.load()) {
                int64_t newest = 0;
                bool have = false;
                for (auto& bp : blocks_) {
                    const BState& s = *bp;
                    const uint64_t h = s.ring.head();
                    int64_t f = 0;
                    bool hf = false;
                    if (h != s.ring.tail()) {
                        f = s.ring.ft_at(h - 1);
                        hf = true;
                    } else if (s.consumed_any) {
                        f = s.consumed_ft;
                        hf = true;
                    }
                    if (hf && (!have || f > newest)) {
                        newest = f;
                        have = true;
                    }
                }
                if (have) {
                    if (!have_cut_ || newest < cut_) {
                        cut_ = newest;
                        have_cut_ = true;
                        cut_reason_ = reason;
                    }
                    stop_on_request_ = true;
                    stop_cut_wall_ = std::chrono::steady_clock::now();
                    stop_cut_pending_.store(true);
                    at_cut = true;
                }
            }
        }
        if (!at_cut) {
            manual_stop_.store(true);  // nothing delivered yet / still armed: no window end to wait for
            unsubscribe_once();        // no new samples; what the rings hold was taken before the stop
            stop_req_.store(true);
        }
    }
    if (writer_.joinable()) writer_.join();
    JObj o;
    o.str("reply", "stop").b("ok", true).raw("footer", footer_.empty() ? "null" : footer_);
    return o.done();
}

}  // namespace tcmcp
