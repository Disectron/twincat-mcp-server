// Where samples come from. The engine never talks to ADS directly: a Source subscribes every block and calls
// Sink::on_sample from its notification thread(s) — one thread per block at a time (SPSC rings rely on it).
#pragma once
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "json.hpp"

namespace tcmcp {

struct BlockSpec {
    uint32_t id = 0;
    uint32_t igrp = 0;
    uint32_t ioffs = 0;
    uint32_t size = 0;
    uint32_t leaves = 1;   // leaf values carried by one sample (for the "events" count)
};

class Sink {
public:
    virtual ~Sink() = default;
    // Hot path. block = index into the block list given to subscribe().
    virtual void on_sample(uint32_t block, int64_t filetime, const uint8_t* data, uint32_t n) = 0;
};

struct SubscribeError {
    int block = -1;           // index of the block that failed (-1: before any block, e.g. no router)
    long code = 0;            // ADS error code when there is one
    std::string message;
};

class Source {
public:
    virtual ~Source() = default;
    // All or nothing: on failure every subscription made so far is removed and the error names the block.
    virtual bool subscribe(const std::vector<BlockSpec>& blocks, int64_t cycle_100ns, int64_t max_delay_100ns,
                           Sink* sink, SubscribeError& err) = 0;
    // After return no further on_sample calls start (calls already running may still finish).
    virtual void unsubscribe() = 0;
    // Describes the target once connected ({"device": ..., "router": ...}); JSON object text.
    virtual std::string describe() const = 0;
};

std::unique_ptr<Source> make_ads_source(const JVal& cfg, std::string& err);
std::unique_ptr<Source> make_sim_source(const JVal& cfg, std::string& err);

}  // namespace tcmcp
