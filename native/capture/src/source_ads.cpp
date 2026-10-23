// ADS source built on Beckhoff's AdsLib (C++ AdsDevice / AdsNotification).
//   Windows + TwinCAT (USE_TWINCAT_ROUTER): AdsLib over Beckhoff's TcAdsDll.dll — the local TwinCAT router,
//     same path as TwinCAT's own tools; works for the local runtime and every routed target.
//   Elsewhere (standalone AdsLib): AdsLib's own AMS/TCP router to a remote IPC (route on the IPC needed).
#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <cstddef>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <vector>

#include "AdsDevice.h"
#include "AdsException.h"
#include "AdsLib.h"
#include "AdsNotificationOOI.h"
#include "source.hpp"

#if defined(USE_TWINCAT_ROUTER) && defined(TCMCP_TCADSDLL_RUNTIME)
#include "TcAdsAPI.h"   // tcmcp_load_tcadsdll
#endif

// The notification callback reads the header in place and copies the sample bytes. Layout checks against
// whichever header this was built with (TwinCAT SDK, tcmcp's declarations, or standalone AdsLib):
static_assert(sizeof(AmsNetId) == 6, "AmsNetId layout");
static_assert(sizeof(AmsAddr) == 8, "AmsAddr layout");
static_assert(sizeof(AdsNotificationAttrib) == 16, "AdsNotificationAttrib layout");
#if defined(USE_TWINCAT_ROUTER)
static_assert(offsetof(AdsNotificationHeader, hNotification) == 0, "TcAdsDll AdsNotificationHeader layout");
static_assert(offsetof(AdsNotificationHeader, nTimeStamp) == 4, "TcAdsDll AdsNotificationHeader layout");
static_assert(offsetof(AdsNotificationHeader, cbSampleSize) == 12, "TcAdsDll AdsNotificationHeader layout");
static_assert(offsetof(AdsNotificationHeader, data) == 16, "TcAdsDll AdsNotificationHeader layout");
#else
static_assert(sizeof(AdsNotificationHeader) == 16, "AdsLib AdsNotificationHeader layout");
#endif

namespace tcmcp {
namespace {

std::atomic<Sink*> g_sink{nullptr};

inline const uint8_t* sample_bytes(const AdsNotificationHeader* h) {
    return reinterpret_cast<const uint8_t*>(h) + 16;
}

constexpr uint32_t kSymVersion = 0xF008;         // ADSIGRP_SYM_VERSION
constexpr uint32_t kSymValByName = 0xF004;       // ADSIGRP_SYM_VALBYNAME
constexpr uint32_t kSymUploadInfo2 = 0xF00F;     // ADSIGRP_SYM_UPLOADINFO2

// One call per sample (TcAdsDll and AdsLib both split batched frames into single samples).
void on_notification(const AmsAddr*, const AdsNotificationHeader* h, uint32_t hUser) {
    Sink* s = g_sink.load(std::memory_order_acquire);
    if (s) s->on_sample(hUser, static_cast<int64_t>(h->nTimeStamp), sample_bytes(h), static_cast<uint32_t>(h->cbSampleSize));
}

std::string ads_error_text(long code);

std::string ads_error(long code) {
    char b[48];
    std::snprintf(b, sizeof b, "ADS error 0x%lX (%ld)", static_cast<unsigned long>(code), code);
    std::string t = ads_error_text(code);
    return t.empty() ? std::string(b) : std::string(b) + ": " + t;
}

std::string ads_error_text(long code) {
    switch (code) {
        case 0x6: return "target port not found (is the PLC runtime started?)";
        case 0x7: return "target machine not found (missing ADS route?)";
        case 0x702: return "invalid index group";
        case 0x703: return "invalid index offset";
        case 0x704: return "reading/writing not permitted";
        case 0x705: return "parameter size not correct";
        case 0x706: return "invalid data";
        case 0x707: return "device not ready (PLC not in RUN?)";
        case 0x713: return "transmission mode not supported";
        case 0x716: return "no more notification handles";
        case 0x717: return "notification size too large";
        case 0x745: return "timeout (ADS route / firewall / target busy)";
        case 0x748: return "ADS port not open (router not running / TcAdsDll not loaded)";
        default: return "";
    }
}

static std::string state_name(int s) {
    static const char* const names[] = {"INVALID", "IDLE", "RESET", "INIT", "START", "RUN", "STOP", "SAVECFG",
                                        "LOADCFG", "POWERFAILURE", "POWERGOOD", "ERROR", "SHUTDOWN", "SUSPEND",
                                        "RESUME", "CONFIG", "RECONFIG", "STOPPING", "INCOMPATIBLE", "EXCEPTION"};
    if (s >= 0 && s < static_cast<int>(sizeof names / sizeof names[0])) return names[s];
    return std::to_string(s);
}

class AdsSource : public Source {
public:
    AdsSource(std::string net_id, uint16_t port, std::string ip, uint32_t timeout_ms)
        : net_id_(std::move(net_id)), port_(port), ip_(std::move(ip)), timeout_ms_(timeout_ms) {}

    ~AdsSource() override { unsubscribe(); }

    bool connect(std::string& err) {
        AmsNetId id = make_AmsNetId(net_id_);
        bool zero = true;
        for (unsigned char c : id.b) zero = zero && c == 0;
        if (zero) {
            err = "invalid AMS Net ID " + net_id_;
            return false;
        }
        try {
            if (!open_port(id, err)) return false;
            DeviceInfo info = dev_->GetDeviceInfo();
            std::ostringstream os;
            os << int(info.version.version) << '.' << int(info.version.revision) << '.' << info.version.build;
            dev_name_ = std::string(info.name, strnlen(info.name, sizeof(info.name)));
            dev_version_ = os.str();
        } catch (const AdsException& ex) {
            err = "ADS connect to " + net_id_ + ":" + std::to_string(port_) + " failed: " + ads_error(ex.errorCode);
            dev_.reset();
            return false;
        } catch (const std::exception& ex) {
            err = std::string("ADS connect failed: ") + ex.what();
            dev_.reset();
            return false;
        }
        return true;
    }

    // A new local AMS port on the router towards the target (no request to the target: it may still be away).
    bool open_port(const AmsNetId& id, std::string& err) {
        dev_.reset(new AdsDevice(ip_, id, port_));
        if (dev_->GetLocalPort() == 0) {
            err = "could not open an ADS port on the local router (TwinCAT router not running?)";
            dev_.reset();
            return false;
        }
        dev_->SetTimeout(timeout_ms_);
        return true;
    }

    bool subscribe(const std::vector<BlockSpec>& blocks, int64_t cycle_100ns, int64_t max_delay_100ns, Sink* sink,
                   SubscribeError& err) override {
        std::lock_guard<std::mutex> lk(m_);
        g_sink.store(sink, std::memory_order_release);
        blocks_ = blocks;
        cycle_ = cycle_100ns;
        max_delay_ = max_delay_100ns;
        for (size_t k = 0; k < blocks.size(); ++k) {
            const BlockSpec& b = blocks[k];
            AdsNotificationAttrib attr{};
            attr.cbLength = b.size;
            attr.nTransMode = ADSTRANS_SERVERCYCLE;
            attr.nMaxDelay = static_cast<uint32_t>(max_delay_100ns);
            attr.nCycleTime = static_cast<uint32_t>(cycle_100ns);
            try {
                notes_.emplace_back(new AdsNotification(*dev_, b.igrp, b.ioffs, attr, &on_notification,
                                                        static_cast<uint32_t>(k)));
            } catch (const AdsException& ex) {
                err.block = static_cast<int>(k);
                err.code = ex.errorCode;
                err.message = ads_error(ex.errorCode);
                drop_locked();
                return false;
            } catch (const std::exception& ex) {
                err.block = static_cast<int>(k);
                err.message = ex.what();
                drop_locked();
                return false;
            }
        }
        return true;
    }

    // Called by the engine's stop path on another thread than the writer, which may be in resubscribe() / reopen() at
    // that moment (review 1 of 0.8.20: two threads clearing notes_, and a reopen freeing the AdsDevice the
    // notification deleters still pointed at) — every use of dev_ / notes_ holds m_; after it nothing is registered
    // again.
    void unsubscribe() override {
        std::lock_guard<std::mutex> lk(m_);
        stopped_ = true;
        drop_locked();
    }

    bool read_version(uint32_t& version, std::string& err) override {
        std::lock_guard<std::mutex> lk(m_);
        if (!dev_) {
            err = "not connected";
            return false;
        }
        uint8_t v = 0;
        uint32_t got = 0;
        const long e = dev_->ReadReqEx2(kSymVersion, 0, 1, &v, &got);
        if (e != 0) {
            err = ads_error(e);
            return false;
        }
        if (got != 1) {
            err = "the symbol version read returned " + std::to_string(got) + " bytes";
            return false;
        }
        version = v;
        return true;
    }

    // "t<compile time>:c<online changes>:u<symbols>,<symbol bytes>,<datatypes>,<datatype bytes>" of the PLC
    // application; "" when any part is not readable.
    std::string identity() override {
        std::lock_guard<std::mutex> lk(m_);
        if (!dev_) return "";
        static const char kTs[] = "TwinCAT_SystemInfoVarList._AppInfo.AppTimestamp";
        static const char kOc[] = "TwinCAT_SystemInfoVarList._AppInfo.OnlineChangeCnt";
        uint32_t ts = 0, oc = 0, got1 = 0, got2 = 0, got3 = 0;
        uint8_t up[64] = {};
        const long e1 = dev_->ReadWriteReqEx2(kSymValByName, 0, sizeof ts, &ts, sizeof kTs, kTs, &got1);
        const long e2 = dev_->ReadWriteReqEx2(kSymValByName, 0, sizeof oc, &oc, sizeof kOc, kOc, &got2);
        const long e3 = dev_->ReadReqEx2(kSymUploadInfo2, 0, sizeof up, up, &got3);
        if (e1 != 0 || e2 != 0 || e3 != 0 || got1 != sizeof ts || got2 != sizeof oc || got3 < 16) return "";
        uint32_t u[4];
        std::memcpy(u, up, sizeof u);
        return "t" + std::to_string(ts) + ":c" + std::to_string(oc) + ":u" + std::to_string(u[0]) + "," +
               std::to_string(u[1]) + "," + std::to_string(u[2]) + "," + std::to_string(u[3]);
    }

    // D13: after RUN→CONFIG→RUN the runtime's notification table is gone; the handles we hold are dead. Drop them
    // (the delete request fails fast while the port is away) and register every block again. All or nothing, like
    // subscribe(): a partial set would record some blocks and silently miss others.
    bool resubscribe(std::string& err) override {
        std::lock_guard<std::mutex> lk(m_);
        if (stopped_) {
            err = "stopped";
            return false;
        }
        if (!dev_ || blocks_.empty()) {
            err = dev_ ? "nothing subscribed" : "no ADS connection (opening it again failed)";
            return false;
        }
        notes_.clear();
        std::vector<std::unique_ptr<AdsNotification>> fresh;
        for (size_t k = 0; k < blocks_.size(); ++k) {
            const BlockSpec& b = blocks_[k];
            AdsNotificationAttrib attr{};
            attr.cbLength = b.size;
            attr.nTransMode = ADSTRANS_SERVERCYCLE;
            attr.nMaxDelay = static_cast<uint32_t>(max_delay_);
            attr.nCycleTime = static_cast<uint32_t>(cycle_);
            try {
                fresh.emplace_back(new AdsNotification(*dev_, b.igrp, b.ioffs, attr, &on_notification,
                                                       static_cast<uint32_t>(k)));
            } catch (const AdsException& ex) {
                err = "block " + std::to_string(b.id) + ": " + ads_error(ex.errorCode);
                return false;
            } catch (const std::exception& ex) {
                err = "block " + std::to_string(b.id) + ": " + ex.what();
                return false;
            }
        }
        notes_.swap(fresh);
        ++resubs_;
        return true;
    }

    uint64_t resubscriptions() const override { return resubs_; }

    bool reopen(std::string& err) override {
        std::lock_guard<std::mutex> lk(m_);
        if (stopped_) {
            err = "stopped";
            return false;
        }
        // review 2 of 0.8.20: the new connection is opened first and the old one kept when that fails (the router may
        // be restarting) — never left without one
        std::unique_ptr<AdsDevice> fresh;
        try {
            fresh.reset(new AdsDevice(ip_, make_AmsNetId(net_id_), port_));
            if (fresh->GetLocalPort() == 0) {
                err = "opening the ADS port again failed: no local port (TwinCAT router not running?)";
                return false;
            }
            fresh->SetTimeout(timeout_ms_);
        } catch (const AdsException& ex) {
            err = "opening the ADS port again failed: " + ads_error(ex.errorCode);
            return false;
        } catch (const std::exception& ex) {
            err = std::string("opening the ADS port again failed: ") + ex.what();
            return false;
        }
        notes_.clear();                      // the delete requests on the old port fail fast; the handles are dead
        dev_.swap(fresh);                    // `fresh` holds the old device now: its local port closes here
        fresh.reset();
        return true;
    }

    // Review 1 of 0.8.20: a ReadState on the PLC port answers 0x6 whenever registering there does — the system service
    // (port 10000) is asked instead: RUN with the PLC port refusing is the N197 case, CONFIG is TwinCAT away.
    int system_state(std::string& text) override {
        std::lock_guard<std::mutex> lk(m_);
        if (!dev_) {
            text = "TwinCAT system state: no ADS connection";
            return -1;
        }
        const long local = dev_->GetLocalPort();
        AmsAddr sys{make_AmsNetId(net_id_), 10000};
        uint16_t ads = 0, dev = 0;
        // a diagnostic: at most 1 s of the attempt (the engine holds its mutex across it, review 2 of 0.8.20)
        dev_->SetTimeout(std::min<uint32_t>(timeout_ms_, 1000));
        const long e = AdsSyncReadStateReqEx(local, &sys, &ads, &dev);
        dev_->SetTimeout(timeout_ms_);
        if (e != 0) {
            text = "TwinCAT system state not readable on this connection: " + ads_error(e) + " (local port " +
                   std::to_string(local) + ")";
            return -1;
        }
        text = "TwinCAT system state on this connection: " + state_name(ads) + " (local port " + std::to_string(local) +
               ")";
        return ads == ADSSTATE_RUN ? 1 : 0;
    }

    std::string describe() const override {
        JObj o;
        o.str("ads_lib", "Beckhoff AdsLib");
#if defined(USE_TWINCAT_ROUTER)
        o.str("router", "TwinCAT (TcAdsDll)");
        if (!dll_.empty()) o.str("tcadsdll", dll_);
#else
        o.str("router", "AdsLib standalone (AMS/TCP)");
#endif
        o.str("target", net_id_ + ":" + std::to_string(port_));
        o.str("device", dev_name_);
        o.str("device_version", dev_version_);
        return o.done();
    }

    std::string dll_;

private:
    std::string net_id_;
    uint16_t port_;
    std::string ip_;
    uint32_t timeout_ms_;
    std::unique_ptr<AdsDevice> dev_;
    std::vector<std::unique_ptr<AdsNotification>> notes_;
    std::string dev_name_, dev_version_;
    std::vector<BlockSpec> blocks_;
    int64_t cycle_ = 10000, max_delay_ = 0;
    uint64_t resubs_ = 0;
    std::mutex m_;               // dev_ / notes_ / blocks_: the writer thread and the stop path
    bool stopped_ = false;

    void drop_locked() {
        // AdsNotification's destructor deletes the device notification (synchronous ADS request).
        notes_.clear();
        g_sink.store(nullptr, std::memory_order_release);
    }
};

}  // namespace

std::unique_ptr<Source> make_ads_source(const JVal& cfg, std::string& err) {
    std::string net_id = cfg.str("ams_net_id");
    int64_t port = cfg.integer("port", 851);
    std::string ip = cfg.str("ip");
    uint32_t timeout_ms = static_cast<uint32_t>(cfg.integer("timeout_ms", 5000));
    std::string dll;
#if defined(USE_TWINCAT_ROUTER) && defined(TCMCP_TCADSDLL_RUNTIME)
    char e[512] = {0}, from[512] = {0};
    if (!tcmcp_load_tcadsdll(cfg.str("tcadsdll_path").c_str(), e, sizeof e, from, sizeof from)) {
        err = e;
        return nullptr;
    }
    dll = from;
#elif !defined(USE_TWINCAT_ROUTER)
    // standalone AdsLib: our own AMS Net ID (the IPC needs a route to it) and the IPC's IP address
    std::string local = cfg.str("local_ams_net_id");
    if (ip.empty()) {
        err = "standalone AdsLib needs \"ip\" (the target's IP address) and \"local_ams_net_id\" with a route on the target";
        return nullptr;
    }
    if (!local.empty()) bhf::ads::SetLocalAddress(make_AmsNetId(local));
#endif
    if (ip.empty()) ip = "127.0.0.1";   // ignored by the TwinCAT router
    auto src = std::make_unique<AdsSource>(net_id, static_cast<uint16_t>(port), ip, timeout_ms);
    src->dll_ = dll;
    if (!src->connect(err)) return nullptr;
    return src;
}

}  // namespace tcmcp
