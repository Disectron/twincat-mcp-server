// The capture engine: rings fed by the ADS callback, one writer thread that merges the blocks by PLC timestamp
// and writes recording format 2 (identical to the Python recorder's), accounting and the signed footer.
#pragma once
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "json.hpp"
#include "ring.hpp"
#include "sha256.hpp"
#include "source.hpp"

namespace tcmcp {

// A start/stop condition on raw bytes: Python resolves the symbol to (block, offset, type) and the literal to a
// number, so the comparison here is on exactly the bytes the PLC sent.
struct Cond {
    bool set = false;
    uint32_t block = 0;
    uint32_t off = 0;
    std::string type;          // bool u8 i8 u16 i16 u32 i32 u64 i64 f32 f64
    std::string op;            // == != > >= < <=
    JVal value;
    bool edge = false;
    int prev = -1;             // edge detection: -1 unknown, 0 false, 1 true
    std::string text;

    bool parse(const JVal& j, size_t nblocks, const std::vector<BlockSpec>& blocks, std::string& err);
    bool test(const uint8_t* d, uint32_t n) const;
    bool fires(const uint8_t* d, uint32_t n);
};

struct BState {
    BlockSpec spec;
    SampleRing ring;
    // writer-owned (read by status under the engine mutex)
    uint64_t samples = 0;
    bool have = false;
    int64_t first_ft = 0, last_ft = 0;
    int64_t min_step = 0;
    uint64_t dups = 0;
    uint64_t late = 0;           // written after a later sample of another block (block was silent, then resumed)
    uint64_t size_mismatch = 0;  // cbSampleSize != block size
    std::vector<std::pair<int64_t, int64_t>> gaps;   // [filetime of the sample before the gap, est. missing], first 20
    // watermark bookkeeping
    uint64_t seen_head = 0;
    std::chrono::steady_clock::time_point progress;
    bool silent = false;
    bool ever_silent = false;
    bool consumed_any = false;
    int64_t consumed_ft = 0;
};

class Engine : public Sink {
public:
    explicit Engine(std::string version) : version_(std::move(version)) {}
    ~Engine() override;

    // Returns the reply line (JSON). On failure nothing is left subscribed and no file is created.
    std::string start(const JVal& cmd);
    std::string status();
    std::string stop(const std::string& reason);
    bool running() const { return started_ && !finished_.load(); }
    bool started() const { return started_; }

    std::function<void(const std::string&)> emit;   // unsolicited event lines (thread-safe in main.cpp)

    void on_sample(uint32_t block, int64_t filetime, const uint8_t* data, uint32_t n) override;

private:
    void writer_loop();
    void process(size_t b, int64_t ft, const uint8_t* d, uint32_t n);
    void commit(size_t b, int64_t ft, const uint8_t* d, uint32_t n);
    int64_t stamp(BState& s, int64_t ft);   // returns the samples missing before this one (0: none)
    void write_line(const std::string& line);
    void write_chain();                      // hash checkpoint over every byte written so far (R9)
    void write_gap(const BState& s, const char* kind, const std::string& from, const std::string& to, int64_t n);
    void note_counter(size_t b, int64_t ft, const uint8_t* d, uint32_t n);   // D11 stall detection
    void close_stall(int64_t to_ft);
    double live_missing_estimate(std::chrono::steady_clock::time_point now) const;   // D14
    void write_t0(int64_t ft);
    void finish_now(const std::string& reason, int64_t end_ft, bool have_end);
    void unsubscribe_once();
    void finish();
    int64_t step_of(const BState& s) const;
    int64_t head_missing(const BState& s) const;
    int64_t inner_missing(const BState& s) const;
    int64_t tail_missing(const BState& s, int64_t end_ft, bool have_end) const;
    int64_t window_end(bool& have) const;
    std::string block_json(const BState& s, int64_t end_ft, bool have_end) const;
    std::string summary_json(bool final_);
    std::string ms(int64_t ft) const;          // ft relative to t0 as exact decimal ms (4 places)

    std::string version_;
    std::unique_ptr<Source> source_;
    std::string source_desc_;
    std::vector<std::unique_ptr<BState>> blocks_;
    std::atomic<bool> accepting_{false};
    std::atomic<uint64_t> after_unsubscribe_{0};

    // config
    std::string file_;
    int64_t cycle_ = 10000, max_delay_ = 0;
    double duration_s_ = 0, post_trigger_s_ = 0, pre_trigger_ms_ = 0;
    uint64_t max_events_ = 0;
    Cond start_when_, stop_when_;
    std::vector<uint8_t> key_;
    std::string key_error_;
    int64_t silent_ns_ = 500000000;
    int64_t resub_after_ns_ = 3000000000;   // D13: all blocks silent this long → ask the source to subscribe again
    int64_t resub_retry_ms_ = 2000;         // then retry at this interval until the target answers
    int64_t writer_stall_ms_ = 0;
    // D11: the recorded cycle counter must advance between samples; frozen values (PLC STOP) are not data
    int stall_block_ = -1;
    uint32_t stall_off_ = 0, stall_size_ = 0;
    int64_t stall_after_ = 0;              // 100 ns: counter unchanged longer than this → stall
    double ring_seconds_ = 0;
    size_t ring_bytes_ = 0;

    // writer state (guarded by mu_ where status reads it)
    mutable std::mutex mu_;
    std::FILE* fh_ = nullptr;
    Sha256 sha_;
    uint64_t lines_ = 0, bytes_ = 0;
    uint64_t chain_lines_ = 0, chains_ = 0, gap_markers_ = 0;
    bool have_t0_ = false;
    int64_t t0_ = 0;
    bool have_ws_ = false;
    int64_t ws_ = 0;                 // window start (all blocks delivering, or trigger − pre_trigger)
    bool ws_from_trigger_ = false;
    bool have_origin_ = false;
    int64_t origin_ = 0;             // first moment every delivering block had a sample (duration counts from here)
    bool have_cut_ = false;
    int64_t cut_ = 0;                // duration / post-trigger end (inclusive)
    std::string cut_reason_;
    bool have_stop_ft_ = false;
    std::atomic<bool> manual_stop_{false};   // stopped by request with no PLC end time (nothing delivered / armed)
    // N56: a stop on request ends at a PLC time — the newest sample delivered when it was asked for. The notifications
    // stay until every block reached it (at most max_delay + 1 s): each block is then complete up to the same end and
    // the tail needs no allowance beyond one period (unsubscribing at once let blocks end max_delay apart)
    std::atomic<bool> stop_cut_pending_{false};
    std::chrono::steady_clock::time_point stop_cut_wall_;
    bool stop_on_request_ = false;
    double stop_wait_ms_ = -1;
    int64_t stop_ft_ = 0;            // stop_when / max_events sample
    bool armed_ = false;
    bool triggered_ = false;
    int64_t trig_ft_ = 0;
    struct Pre {
        uint32_t b;
        int64_t ft;
        uint32_t n;
        std::vector<uint8_t> d;
    };
    std::deque<Pre> pre_;
    // R41: a level start_when waits until every block delivers (or 200 ms of PLC time): fired on block 0's first
    // sample it put the window start before block 1's subscription began (missing_head 1 on a lossless capture)
    std::vector<char> armed_seen_;
    size_t armed_seen_n_ = 0;
    int64_t armed_t0_ = 0;
    bool have_armed_t0_ = false;
    // N61: the level condition was already true during that warm-up — a later trigger reports its delay, a trigger
    // that never fires says it was true then (and false afterwards) instead of nothing
    bool have_warm_true_ = false;
    int64_t warm_true_ft_ = 0;
    bool warm_lapsed_ = false;   // … and false on a cycle every block delivered: a later fire is not that warm-up truth
    uint64_t samples_ = 0, events_ = 0, after_limit_ = 0;
    int64_t last_emitted_ = 0;
    bool any_emitted_ = false;
    size_t max_backlog_ = 0;
    std::string stop_reason_;
    std::vector<std::string> errors_;
    // stall state (writer-owned)
    std::vector<uint8_t> stall_prev_;
    bool stall_have_prev_ = false;
    int64_t stall_adv_ft_ = 0;             // last sample where the counter advanced
    uint64_t stall_rep_ = 0;               // repeated samples since then
    bool stall_open_ = false;
    uint64_t stalls_ = 0, stalled_samples_ = 0;
    int64_t stall_100ns_ = 0;
    std::vector<std::pair<int64_t, int64_t>> stall_list_;   // [from, to] first 20
    // resubscription state (writer-owned)
    std::chrono::steady_clock::time_point last_resub_try_;
    bool resub_tried_ = false;
    uint64_t resub_failures_ = 0;
    std::string resub_error_;
    std::vector<std::string> resub_notes_;
    std::string row_;                // reused line buffer
    std::chrono::steady_clock::time_point t_start_, t_first_;
    bool have_first_wall_ = false;
    std::chrono::steady_clock::time_point last_flush_;
    std::string footer_;

    std::atomic<bool> stop_req_{false};
    std::atomic<bool> finish_req_{false};
    std::atomic<bool> finished_{false};
    bool started_ = false;
    std::once_flag unsub_once_;
    std::thread writer_;
};

std::string utc_iso_from_filetime(int64_t ft);
std::string utc_iso_now();

}  // namespace tcmcp
