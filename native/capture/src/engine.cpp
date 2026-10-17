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

void Engine::stamp(BState& s, int64_t ft) {
    if (s.have) {
        int64_t step = ft - s.last_ft;
        if (step <= 0) {
            ++s.dups;
        } else {
            if (!s.min_step || step < s.min_step) s.min_step = step;
            if (s.min_step && 2 * step > 3 * s.min_step && s.gaps.size() < 20)
                s.gaps.emplace_back(s.last_ft, std::llround(double(step) / double(s.min_step)) - 1);
        }
    } else {
        s.first_ft = ft;
        s.have = true;
    }
    s.last_ft = ft;
    ++s.samples;
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
    stamp(s, ft);
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
        while (!pre_.empty() && pre_.front().ft < ft - pre100) pre_.pop_front();
        if (start_when_.block == b && start_when_.fires(d, n)) {
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
                commit(p.b, p.ft, p.d.data(), p.n);   // p.d holds min(n, block size) bytes
            }
        }
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
        size_t done = 0;
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
                        if (duration_s_ > 0) {
                            cut_ = o + std::llround(duration_s_ * 1e7);
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
            // limits on this PC's clock (the PLC stopped delivering: nothing would ever reach the cut)
            if (!finish_req_.load() && !stopping && duration_s_ > 0) {
                const auto grace = std::chrono::seconds(1) + std::chrono::nanoseconds(max_delay_ * 100);
                const auto dur = std::chrono::nanoseconds(static_cast<int64_t>(duration_s_ * 1e9));
                if (!have_first_wall_ && backlog == 0 && now - t_start_ > dur + std::chrono::seconds(5)) {
                    finish_now("duration elapsed with no data (PLC not running? symbols never updated?)", 0, false);
                } else if (have_first_wall_ && all_silent && backlog == 0 && now - t_first_ > dur + grace && have_cut_) {
                    char r[160];
                    std::snprintf(r, sizeof r, "duration %gs: the PLC stopped delivering at %s ms (rest of the window counted as missing_tail)",
                                  duration_s_, have_t0_ ? ms(last_emitted_).c_str() : "?");
                    finish_now(r, cut_, true);
                }
            }
            if (fh_ && now - last_flush_ > std::chrono::seconds(1)) {
                std::fflush(fh_);
                last_flush_ = now;
            }
        }
        if (finish_req_.load()) break;
        if (stopping && backlog == 0 && done == 0) break;
        if (done == 0) std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
    unsubscribe_once();
    {
        std::lock_guard<std::mutex> lk(mu_);
        for (auto& bp : blocks_) after_limit_ += bp->ring.size();
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
    return std::max<int64_t>(0, (end - s.last_ft) / step_of(s) - 1);   // one period of slack, as the Python recorder
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
    const int64_t miss = head + gaps + tail;
    const double comp = (samples_ + miss) ? double(samples_) / double(samples_ + static_cast<uint64_t>(miss)) : 0.0;
    const bool pass = samples_ > 0 && miss == 0 && ovf == 0 && mismatch == 0 && (!start_when_.set || triggered_);
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
    f.raw("window_start_ms", have_ws_ && have_t0_ ? ms(ws_) : "null");
    f.raw("window_end_ms", have_end && have_t0_ ? ms(end) : "null");
    if (triggered_) f.raw("triggered_at_ms", ms(trig_ft_));
    f.d("completeness", comp, 8).str("verdict_hint", pass ? "PASS" : "FAIL");
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
    JObj o;
    o.str("reply", "status").b("ok", true).str("engine", "native").b("active", started_ && !finished_.load())
        .b("armed", armed_).raw("triggered_at_ms", triggered_ && have_t0_ ? ms(trig_ft_) : "null")
        .u("samples", samples_).u("events", events_).i("missing_samples", miss).i("missing_head", head)
        .i("missing_gaps", gaps).i("missing_tail", tail).u("overflow_samples", ovf).u("backlog", backlog)
        .u("max_backlog", max_backlog_).u("ring_capacity_samples", blocks_.empty() ? 0 : blocks_[0]->ring.capacity())
        .d("ring_seconds", ring_seconds_, 6).u("ring_bytes", ring_bytes_).u("bytes_written", bytes_).u("lines", lines_)
        .raw("plc_span_s", any_emitted_ && have_t0_ ? jnum(double(last_emitted_ - t0_) / 1e7, 12) : "0")
        .d("elapsed_s", elapsed, 6).u("silent_blocks", silent).u("after_limit", after_limit_);
    o.d("completeness", (samples_ + miss) ? double(samples_) / double(samples_ + uint64_t(miss)) : 0.0, 8);
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
        {
            std::lock_guard<std::mutex> lk(mu_);
            if (stop_reason_.empty()) stop_reason_ = reason;
        }
        unsubscribe_once();        // no new samples; what the rings hold was taken before the stop
        stop_req_.store(true);
    }
    if (writer_.joinable()) writer_.join();
    JObj o;
    o.str("reply", "stop").b("ok", true).raw("footer", footer_.empty() ? "null" : footer_);
    return o.done();
}

}  // namespace tcmcp
