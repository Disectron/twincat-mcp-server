// Simulated PLC for tests and CI (no TwinCAT needed). Every block delivers one sample per cycle with an exact
// FILETIME; the bytes are little-endian UDINT counters (word i of cycle c = c + i), so a 4-byte block behaves
// like _TaskInfo[n].CycleCount. Faults are injected on request:
//   "sim": {"start_ft": <FILETIME>, "speed": <x real time, 0 = flat out>, "stop_after_cycles": n,
//           "drop": [[block, first_cycle, last_cycle], ...], "late": [[block, cycles], ...], "fail_block": k,
//           "max_cycles": n,
//           "batch": [[block, cycles], ...]       (that block's samples are held and delivered together every `cycles`
//                                                  cycles, after the other blocks' — ADS batching with max_delay)
//           "skew": [[block, cycles], ...]        (that block's notifications run `cycles` ahead and arrive first in
//                                                  each cycle: its sample of cycle c+n comes before the others' of c),
//           "freeze": [first_cycle, last_cycle]   (PLC STOP: samples keep coming, every value frozen),
//           "outage": [first_cycle, last_cycle],  (TwinCAT in CONFIG: no samples; resubscribe() succeeds only after
//                                                  last_cycle, as the runtime answers again)
//           "program_change_at": cycle,           (an online change: once a sample of that cycle is delivered, the
//                                                  symbol version reads 2 and the online-change count 1)
//           "rebuild_in_outage": true,            (the runtime comes back from the outage with another build)
//           "stale_port_after_outage": true,      (N197: after the outage re-registering keeps failing with ADS 0x6
//                                                  until reopen() opened a new connection; the system state reads RUN)
//           "system_state_in_outage": "unreadable", (the system state cannot be read during the outage; default: it
//                                                  reads CONFIG)
//           "reopen_fails": n,                    (the first n reopen() calls fail; the old connection is kept)
//           "version_after_outage": n,            (the runtime comes back with symbol version n)
//           "version_at_start": n,                (the symbol version before anything changes, default 1)
//           "version_read_fails": true,           (the runtime refuses every symbol version read)
//           "version_fail_from": cycle,           (symbol version reads fail once that cycle is delivered)
//           "hold_for_check": [cycle, ...]}       (that cycle is not delivered before a symbol version read was answered
//                                                  after the cycle before it: a test that times an event against the
//                                                  engine's version checks does not depend on the writer thread being
//                                                  scheduled in time — on a loaded runner it can be late by 500 ms)
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstring>
#include <thread>
#include <vector>

#include "source.hpp"

namespace tcmcp {
namespace {

struct Range {
    int64_t block, first, last;
};

class SimSource : public Source {
public:
    explicit SimSource(const JVal& sim) {
        start_ft_ = sim.integer("start_ft", 0);
        if (start_ft_ <= 0) {
            using namespace std::chrono;
            auto us = duration_cast<microseconds>(system_clock::now().time_since_epoch()).count();
            start_ft_ = 116444736000000000LL + us * 10;
        }
        speed_ = sim.num("speed", 1.0);
        stop_after_ = sim.integer("stop_after_cycles", -1);
        max_cycles_ = sim.integer("max_cycles", -1);
        fail_block_ = sim.integer("fail_block", -1);
        if (const JVal* d = sim.get("drop"))
            for (auto& r : d->a)
                if (r.a.size() == 3) drops_.push_back({r.a[0].i, r.a[1].i, r.a[2].i});
        if (const JVal* l = sim.get("late"))
            for (auto& r : l->a)
                if (r.a.size() == 2) late_.push_back({r.a[0].i, r.a[1].i, 0});
        if (const JVal* bt = sim.get("batch"))
            for (auto& r : bt->a)
                if (r.a.size() == 2 && r.a[1].i > 0) batch_.push_back({r.a[0].i, r.a[1].i, 0});
        if (const JVal* k = sim.get("skew"))
            for (auto& r : k->a)
                if (r.a.size() == 2) skew_.push_back({r.a[0].i, r.a[1].i, 0});
        if (const JVal* f = sim.get("freeze"))
            if (f->a.size() == 2) { freeze_from_ = f->a[0].i; freeze_to_ = f->a[1].i; }
        if (const JVal* o = sim.get("outage"))
            if (o->a.size() == 2) { outage_from_ = o->a[0].i; outage_to_ = o->a[1].i; }
        program_change_at_ = sim.integer("program_change_at", -1);
        if (const JVal* r = sim.get("rebuild_in_outage")) rebuild_in_outage_ = r->t == JVal::Bool && r->b;
        if (const JVal* r = sim.get("stale_port_after_outage")) stale_port_ = r->t == JVal::Bool && r->b;
        sys_unreadable_in_outage_ = sim.str("system_state_in_outage") == "unreadable";
        reopen_fails_ = sim.integer("reopen_fails", 0);
        version_after_outage_ = sim.integer("version_after_outage", -1);
        version_at_start_ = sim.integer("version_at_start", 1);
        if (const JVal* r = sim.get("version_read_fails")) version_read_fails_ = r->t == JVal::Bool && r->b;
        version_fail_from_ = sim.integer("version_fail_from", -1);
        if (const JVal* h = sim.get("hold_for_check"))
            for (auto& c : h->a) holds_.push_back(c.i);
    }

    bool read_version(uint32_t& v, std::string& err) override {
        const int64_t m = max_emitted_.load();
        if (version_read_fails_) {
            err = "ADS error 0x701 (1793): service is not supported by the server (simulated)";
            return false;
        }
        if (version_fail_from_ >= 0 && m >= version_fail_from_) {
            err = "ADS error 0x745 (1861): timeout elapsed (simulated)";
            return false;
        }
        if (away()) {
            err = "ADS error 0x6 (6): target port not found (simulated outage)";
            return false;
        }
        int64_t ver = version_at_start_;
        if (program_change_at_ >= 0 && m >= program_change_at_) ver = version_at_start_ + 1;
        if (back_from_outage()) {
            if (version_after_outage_ >= 0) ver = version_after_outage_;
            else if (rebuild_in_outage_) ver = version_at_start_ + 1;
        }
        v = static_cast<uint32_t>(ver);
        int64_t seen = read_ok_through_.load();
        while (m > seen && !read_ok_through_.compare_exchange_weak(seen, m)) {}
        return true;
    }

    std::string identity() override {
        if (away() || version_read_fails_) return "";
        if (back_from_outage() && rebuild_in_outage_) return "t2:c0:u9,900,4,400";
        const bool changed = program_change_at_ >= 0 && max_emitted_.load() >= program_change_at_;
        return changed ? "t1:c1:u9,900,4,400" : "t1:c0:u9,900,4,400";
    }
    ~SimSource() override { unsubscribe(); }

    bool subscribe(const std::vector<BlockSpec>& blocks, int64_t cycle_100ns, int64_t, Sink* sink,
                   SubscribeError& err) override {
        for (size_t k = 0; k < blocks.size(); ++k) {
            if (static_cast<int64_t>(k) == fail_block_ || blocks[k].igrp == 0xDEAD) {
                err.block = static_cast<int>(k);
                err.code = 0x702;
                err.message = "ADS error 0x702 (1794): invalid index group (simulated)";
                return false;
            }
        }
        blocks_ = blocks;
        cycle_ = cycle_100ns > 0 ? cycle_100ns : 10000;
        sink_ = sink;
        run_.store(true);
        th_ = std::thread([this] { loop(); });
        return true;
    }

    void unsubscribe() override {
        if (run_.exchange(false) && th_.joinable()) th_.join();
        else if (th_.joinable()) th_.join();
    }

    bool resubscribe(std::string& err) override {
        // the "runtime" is back once the outage is over; until then the request fails like ADS error 0x6
        if (outage_from_ >= 0 && cycle_now_.load() <= outage_to_) {
            err = "ADS error 0x6 (6): target port not found (simulated outage)";
            return false;
        }
        if (stale_port_) {
            err = "ADS error 0x6 (6): target port not found (simulated stale connection)";
            return false;
        }
        dead_.store(false);
        ++resubs_;
        return true;
    }

    uint64_t resubscriptions() const override { return resubs_; }

    bool reopen(std::string& err) override {
        if (reopen_fails_ > 0) {
            --reopen_fails_;
            err = "opening the ADS port again failed: ADS error 0x748 (1864): port not open (simulated)";
            return false;
        }
        if (outage_from_ >= 0 && cycle_now_.load() > outage_to_) stale_port_ = false;   // a new connection reaches it
        ++reopens_;
        return true;
    }

    int system_state(std::string& text) override {
        if (away()) {
            if (sys_unreadable_in_outage_) {
                text = "TwinCAT system state not readable on this connection: ADS error 0x745 (1861): timeout "
                       "elapsed (simulated)";
                return -1;
            }
            text = "TwinCAT system state on this connection: CONFIG (simulated)";
            return 0;
        }
        text = "TwinCAT system state on this connection: RUN (simulated)";
        return 1;
    }

    std::string describe() const override {
        JObj o;
        o.str("router", "simulator");
        o.str("device", "tcmcp-sim");
        o.d("speed", speed_);
        return o.done();
    }

private:
    bool away() const {          // the runtime does not answer during the outage
        const int64_t c = cycle_now_.load();
        return outage_from_ >= 0 && c >= outage_from_ && c <= outage_to_;
    }
    bool back_from_outage() const { return outage_to_ >= 0 && cycle_now_.load() > outage_to_; }

    bool dropped(int64_t b, int64_t c) const {
        for (auto& r : drops_)
            if (r.block == b && c >= r.first && c <= r.last) return true;
        for (auto& r : late_)
            if (r.block == b && c < r.first) return true;
        return false;
    }

    void loop() {
        std::vector<std::vector<uint8_t>> buf(blocks_.size());
        for (size_t k = 0; k < blocks_.size(); ++k) buf[k].assign(blocks_[k].size, 0);
        auto t0 = std::chrono::steady_clock::now();
        int64_t c = 0;
        while (run_.load(std::memory_order_relaxed)) {
            if ((stop_after_ >= 0 && c >= stop_after_) || (max_cycles_ >= 0 && c >= max_cycles_)) {
                std::this_thread::sleep_for(std::chrono::milliseconds(5));   // PLC stopped: no more samples
                continue;
            }
            if (c > 0 && std::find(holds_.begin(), holds_.end(), c) != holds_.end()) {
                // hold_for_check: a version read answered after cycle c-1 first (it may be its verification)
                while (run_.load(std::memory_order_relaxed) && read_ok_through_.load() < c - 1)
                    std::this_thread::sleep_for(std::chrono::milliseconds(1));
                if (!run_.load(std::memory_order_relaxed)) break;
            }
            const int64_t ft = start_ft_ + c * cycle_;
            cycle_now_.store(c);
            if (outage_from_ >= 0 && c == outage_from_) dead_.store(true);   // the runtime went away: handles dead
            // frozen PLC: the notification keeps coming, the values stay at the last cycle before the freeze
            auto emit = [&](size_t k, int64_t cyc) {
                if (dropped(static_cast<int64_t>(k), cyc) || dead_.load()) return;
                if (cyc > max_emitted_.load()) max_emitted_.store(cyc);    // before delivery: a read after it sees it
                const int64_t cv = (freeze_from_ >= 0 && cyc >= freeze_from_ && cyc <= freeze_to_) ? freeze_from_ - 1 : cyc;
                auto& b = buf[k];
                size_t words = b.size() / 4;
                for (size_t i = 0; i < words; ++i) {
                    uint32_t v = static_cast<uint32_t>(cv + static_cast<int64_t>(i));
                    std::memcpy(&b[i * 4], &v, 4);
                }
                for (size_t i = words * 4; i < b.size(); ++i) b[i] = static_cast<uint8_t>(cv);
                sink_->on_sample(static_cast<uint32_t>(k), start_ft_ + cyc * cycle_, b.data(), static_cast<uint32_t>(b.size()));
            };
            // skewed blocks first, ahead by their skew (on the first cycle they also deliver the cycles they skip)
            for (auto& sk : skew_) {
                if (sk.block < 0 || sk.block >= static_cast<int64_t>(blocks_.size())) continue;
                for (int64_t cyc = (c == 0 ? 0 : c + sk.first); cyc <= c + sk.first; ++cyc) emit(static_cast<size_t>(sk.block), cyc);
            }
            for (size_t k = 0; k < blocks_.size(); ++k) {
                bool skewed = false, batched = false;
                for (auto& sk : skew_) skewed = skewed || sk.block == static_cast<int64_t>(k);
                for (auto& bt : batch_) batched = batched || bt.block == static_cast<int64_t>(k);
                if (!skewed && !batched) emit(k, c);
            }
            // batched blocks: every `cycles` cycles, the held cycles in order, after the others of this cycle
            for (auto& bt : batch_) {
                if (bt.block < 0 || bt.block >= static_cast<int64_t>(blocks_.size())) continue;
                if ((c + 1) % bt.first == 0)
                    for (int64_t cyc = c + 1 - bt.first; cyc <= c; ++cyc) emit(static_cast<size_t>(bt.block), cyc);
            }
            (void)ft;
            ++c;
            // pace every cycle when a cycle lasts ≥ 1 ms of wall time (a 10 ms PLC must not arrive in bursts of 64),
            // in groups of 64 otherwise
            const bool slow = speed_ > 0 && double(cycle_) * 100.0 / speed_ >= 1e6;
            if (slow || (c & 63) == 0) {
                if (speed_ > 0) {
                    // sim time c*cycle (100 ns units) runs `speed` times faster than the wall clock
                    auto due = t0 + std::chrono::nanoseconds(static_cast<int64_t>(c * cycle_ * 100 / speed_));
                    auto now = std::chrono::steady_clock::now();
                    if (due > now) std::this_thread::sleep_for(due - now);
                } else {
                    std::this_thread::yield();
                }
            }
        }
    }

    int64_t start_ft_ = 0, cycle_ = 10000, stop_after_ = -1, max_cycles_ = -1, fail_block_ = -1;
    int64_t freeze_from_ = -1, freeze_to_ = -1, outage_from_ = -1, outage_to_ = -1, program_change_at_ = -1;
    bool rebuild_in_outage_ = false, version_read_fails_ = false;
    int64_t version_after_outage_ = -1, version_at_start_ = 1, version_fail_from_ = -1;
    std::atomic<int64_t> max_emitted_{-1};
    std::atomic<int64_t> read_ok_through_{-1};          // the newest cycle a successful version read was taken after
    std::vector<int64_t> holds_;                        // hold_for_check
    std::atomic<int64_t> cycle_now_{0};
    std::atomic<bool> dead_{false};
    bool stale_port_ = false;
    bool sys_unreadable_in_outage_ = false;
    int64_t reopen_fails_ = 0;
    uint64_t reopens_ = 0;
    uint64_t resubs_ = 0;
    double speed_ = 1.0;
    std::vector<Range> drops_, late_, skew_, batch_;   // skew_: {block, cycles ahead, unused}; batch_: {block, cycles}
    std::vector<BlockSpec> blocks_;
    Sink* sink_ = nullptr;
    std::atomic<bool> run_{false};
    std::thread th_;
};

}  // namespace

std::unique_ptr<Source> make_sim_source(const JVal& cfg, std::string&) {
    static const JVal empty = [] { JVal v; v.t = JVal::Obj; return v; }();
    const JVal* sim = cfg.get("sim");
    return std::make_unique<SimSource>(sim && sim->t == JVal::Obj ? *sim : empty);
}

}  // namespace tcmcp
