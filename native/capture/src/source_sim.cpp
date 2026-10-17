// Simulated PLC for tests and CI (no TwinCAT needed). Every block delivers one sample per cycle with an exact
// FILETIME; the bytes are little-endian UDINT counters (word i of cycle c = c + i), so a 4-byte block behaves
// like _TaskInfo[n].CycleCount. Faults are injected on request:
//   "sim": {"start_ft": <FILETIME>, "speed": <x real time, 0 = flat out>, "stop_after_cycles": n,
//           "drop": [[block, first_cycle, last_cycle], ...], "late": [[block, cycles], ...], "fail_block": k,
//           "max_cycles": n}
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

    std::string describe() const override {
        JObj o;
        o.str("router", "simulator");
        o.str("device", "tcmcp-sim");
        o.d("speed", speed_);
        return o.done();
    }

private:
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
            const int64_t ft = start_ft_ + c * cycle_;
            for (size_t k = 0; k < blocks_.size(); ++k) {
                if (dropped(static_cast<int64_t>(k), c)) continue;
                auto& b = buf[k];
                size_t words = b.size() / 4;
                for (size_t i = 0; i < words; ++i) {
                    uint32_t v = static_cast<uint32_t>(c + static_cast<int64_t>(i));
                    std::memcpy(&b[i * 4], &v, 4);
                }
                for (size_t i = words * 4; i < b.size(); ++i) b[i] = static_cast<uint8_t>(c);
                sink_->on_sample(static_cast<uint32_t>(k), ft, b.data(), static_cast<uint32_t>(b.size()));
            }
            ++c;
            if ((c & 63) == 0) {
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
    double speed_ = 1.0;
    std::vector<Range> drops_, late_;
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
