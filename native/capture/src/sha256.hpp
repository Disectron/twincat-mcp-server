// SHA-256 and HMAC-SHA256 (FIPS 180-4 / RFC 2104). Self-contained so the capture service needs no crypto library.
#pragma once
#include <array>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

namespace tcmcp {

class Sha256 {
public:
    Sha256() { reset(); }
    void reset() {
        static const uint32_t init[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                         0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
        std::memcpy(h_, init, sizeof h_);
        len_ = 0;
        fill_ = 0;
    }
    void update(const void* data, size_t n) {
        const uint8_t* p = static_cast<const uint8_t*>(data);
        len_ += n;
        while (n) {
            size_t take = 64 - fill_;
            if (take > n) take = n;
            std::memcpy(buf_ + fill_, p, take);
            fill_ += take; p += take; n -= take;
            if (fill_ == 64) { block(buf_); fill_ = 0; }
        }
    }
    void update(const std::string& s) { update(s.data(), s.size()); }
    std::array<uint8_t, 32> digest() {
        Sha256 c = *this;               // finalize a copy: the running hash stays usable
        uint64_t bits = c.len_ * 8;
        uint8_t pad = 0x80;
        c.update(&pad, 1);
        uint8_t z = 0;
        while (c.fill_ != 56) c.update(&z, 1);
        uint8_t lb[8];
        for (int i = 0; i < 8; ++i) lb[7 - i] = static_cast<uint8_t>(bits >> (8 * i));
        c.update(lb, 8);
        std::array<uint8_t, 32> out{};
        for (int i = 0; i < 8; ++i)
            for (int j = 0; j < 4; ++j) out[i * 4 + j] = static_cast<uint8_t>(c.h_[i] >> (24 - 8 * j));
        return out;
    }
    std::string hexdigest() { return hex(digest().data(), 32); }

    static std::string hex(const uint8_t* d, size_t n) {
        static const char* x = "0123456789abcdef";
        std::string s(n * 2, '0');
        for (size_t i = 0; i < n; ++i) { s[2 * i] = x[d[i] >> 4]; s[2 * i + 1] = x[d[i] & 15]; }
        return s;
    }

private:
    static uint32_t rotr(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }
    void block(const uint8_t* p) {
        static const uint32_t k[64] = {
            0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
            0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
            0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
            0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
            0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
            0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
            0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
            0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
        uint32_t w[64];
        for (int i = 0; i < 16; ++i)
            w[i] = (uint32_t(p[4 * i]) << 24) | (uint32_t(p[4 * i + 1]) << 16) | (uint32_t(p[4 * i + 2]) << 8) | uint32_t(p[4 * i + 3]);
        for (int i = 16; i < 64; ++i) {
            uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
            uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16] + s0 + w[i - 7] + s1;
        }
        uint32_t a = h_[0], b = h_[1], c = h_[2], d = h_[3], e = h_[4], f = h_[5], g = h_[6], h = h_[7];
        for (int i = 0; i < 64; ++i) {
            uint32_t S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
            uint32_t ch = (e & f) ^ (~e & g);
            uint32_t t1 = h + S1 + ch + k[i] + w[i];
            uint32_t S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
            uint32_t mj = (a & b) ^ (a & c) ^ (b & c);
            uint32_t t2 = S0 + mj;
            h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
        }
        h_[0] += a; h_[1] += b; h_[2] += c; h_[3] += d; h_[4] += e; h_[5] += f; h_[6] += g; h_[7] += h;
    }
    uint32_t h_[8];
    uint64_t len_;
    size_t fill_;
    uint8_t buf_[64];
};

inline std::string hmac_sha256_hex(const std::vector<uint8_t>& key_in, const std::string& msg) {
    std::vector<uint8_t> key = key_in;
    if (key.size() > 64) {
        Sha256 k; k.update(key.data(), key.size());
        auto d = k.digest();
        key.assign(d.begin(), d.end());
    }
    key.resize(64, 0);
    uint8_t ipad[64], opad[64];
    for (int i = 0; i < 64; ++i) { ipad[i] = key[i] ^ 0x36; opad[i] = key[i] ^ 0x5c; }
    Sha256 in; in.update(ipad, 64); in.update(msg);
    auto ih = in.digest();
    Sha256 out; out.update(opad, 64); out.update(ih.data(), 32);
    return out.hexdigest();
}

// Appends the base64 (RFC 4648, with padding — what Python's base64.b64encode produces) of d[0..n) to out.
inline void base64_append(std::string& out, const uint8_t* d, size_t n) {
    static const char* t = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    size_t pos = out.size();
    out.resize(pos + ((n + 2) / 3) * 4);
    char* o = &out[pos];
    size_t i = 0;
    for (; i + 2 < n; i += 3) {
        uint32_t v = (uint32_t(d[i]) << 16) | (uint32_t(d[i + 1]) << 8) | d[i + 2];
        *o++ = t[v >> 18]; *o++ = t[(v >> 12) & 63]; *o++ = t[(v >> 6) & 63]; *o++ = t[v & 63];
    }
    if (i < n) {
        uint32_t v = uint32_t(d[i]) << 16;
        if (i + 1 < n) v |= uint32_t(d[i + 1]) << 8;
        *o++ = t[v >> 18]; *o++ = t[(v >> 12) & 63];
        *o++ = (i + 1 < n) ? t[(v >> 6) & 63] : '=';
        *o++ = '=';
    }
}

inline std::string base64(const uint8_t* d, size_t n) {
    std::string s;
    base64_append(s, d, n);
    return s;
}

}  // namespace tcmcp
