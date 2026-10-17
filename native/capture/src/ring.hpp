// Lock-free single-producer / single-consumer ring of fixed-size sample slots, one per notification block.
// Producer = the ADS router's notification thread (one thread per handle), consumer = the writer thread.
// push() is the whole hot path: no allocation, no lock, no system call. A full ring drops the NEW sample and
// counts it (overflow is reported, never silent).
#pragma once
#include <atomic>
#include <cstdint>
#include <cstring>
#include <vector>

namespace tcmcp {

struct OverflowInterval {
    int64_t first_ft = 0;
    int64_t last_ft = 0;
    uint64_t count = 0;
};

class SampleRing {
public:
    static constexpr size_t kMaxIntervals = 16;

    void init(size_t slots, uint32_t sample_bytes) {
        cap_ = slots < 2 ? 2 : slots;
        bytes_ = sample_bytes;
        stride_ = 16 + ((sample_bytes + 7u) & ~size_t(7));    // [i64 ft][u32 n][u32 pad][data, 8-aligned]
        buf_.assign(cap_ * stride_, 0);
    }
    size_t capacity() const { return cap_; }
    size_t stride() const { return stride_; }
    uint32_t sample_bytes() const { return bytes_; }

    // ---------------------------------------------------------------- producer side
    // Returns false (and counts) when the ring is full.
    bool push(int64_t ft, const uint8_t* data, uint32_t n) {
        const uint64_t h = head_.load(std::memory_order_relaxed);
        const uint64_t t = tail_.load(std::memory_order_acquire);
        if (h - t >= cap_) {
            note_overflow(ft);
            return false;
        }
        if (in_overflow_) close_interval();
        uint8_t* s = &buf_[(h % cap_) * stride_];
        uint32_t take = n < bytes_ ? n : bytes_;
        std::memcpy(s, &ft, 8);
        std::memcpy(s + 8, &n, 4);
        std::memcpy(s + 16, data, take);
        if (take < bytes_) std::memset(s + 16 + take, 0, bytes_ - take);
        head_.store(h + 1, std::memory_order_release);
        return true;
    }

    // ---------------------------------------------------------------- consumer side
    uint64_t head() const { return head_.load(std::memory_order_acquire); }
    uint64_t tail() const { return tail_.load(std::memory_order_relaxed); }
    size_t size() const { return static_cast<size_t>(head() - tail()); }
    // Slot k (absolute index, tail <= k < head): valid until pop() moves the tail past it.
    int64_t ft_at(uint64_t k) const {
        int64_t ft;
        std::memcpy(&ft, &buf_[(k % cap_) * stride_], 8);
        return ft;
    }
    uint32_t n_at(uint64_t k) const {
        uint32_t n;
        std::memcpy(&n, &buf_[(k % cap_) * stride_ + 8], 4);
        return n;
    }
    const uint8_t* data_at(uint64_t k) const { return &buf_[(k % cap_) * stride_ + 16]; }
    void pop() { tail_.store(tail_.load(std::memory_order_relaxed) + 1, std::memory_order_release); }

    // overflow accounting (written by the producer; read by the writer/status — counts are atomics, the closed
    // interval list is published through intervals_done_)
    uint64_t overflow() const { return overflow_.load(std::memory_order_relaxed); }
    size_t intervals_done() const { return intervals_done_.load(std::memory_order_acquire); }
    const OverflowInterval& interval(size_t k) const { return intervals_[k]; }
    // The interval still open when the stream ended (read only after the producer stopped).
    bool open_interval(OverflowInterval& out) const {
        if (!in_overflow_) return false;
        out = cur_;
        return true;
    }
    uint64_t intervals_total() const { return intervals_total_.load(std::memory_order_relaxed); }

private:
    void note_overflow(int64_t ft) {
        overflow_.fetch_add(1, std::memory_order_relaxed);
        if (!in_overflow_) {
            in_overflow_ = true;
            cur_ = OverflowInterval{ft, ft, 0};
        }
        cur_.last_ft = ft;
        ++cur_.count;
    }
    void close_interval() {
        in_overflow_ = false;
        intervals_total_.fetch_add(1, std::memory_order_relaxed);
        size_t k = intervals_done_.load(std::memory_order_relaxed);
        if (k < kMaxIntervals) {
            intervals_[k] = cur_;
            intervals_done_.store(k + 1, std::memory_order_release);
        }
    }

    std::vector<uint8_t> buf_;
    size_t cap_ = 0;
    size_t stride_ = 0;
    uint32_t bytes_ = 0;
    alignas(64) std::atomic<uint64_t> head_{0};
    alignas(64) std::atomic<uint64_t> tail_{0};
    alignas(64) std::atomic<uint64_t> overflow_{0};
    std::atomic<uint64_t> intervals_total_{0};
    std::atomic<size_t> intervals_done_{0};
    bool in_overflow_ = false;
    OverflowInterval cur_;
    OverflowInterval intervals_[kMaxIntervals];
};

}  // namespace tcmcp
