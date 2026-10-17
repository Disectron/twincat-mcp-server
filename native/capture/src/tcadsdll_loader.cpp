// tcmcp-capture: binds the TcAdsDll functions declared in tcadsdll/TcAdsAPI.h to Beckhoff's TcAdsDll.dll at
// run time. Used only when building WITHOUT the TwinCAT SDK (the default; see CMakeLists.txt).
#ifdef _WIN32
#include <windows.h>

#include <cstdio>
#include <cstring>
#include <string>

#include "TcAdsAPI.h"

namespace
{
HMODULE g_dll = nullptr;

#define TCMCP_FN(name, ret, params) using name##_t = ret(TCADSDLL_API *) params; name##_t p_##name = nullptr;
TCMCP_FN(AdsGetDllVersion, long, (void))
TCMCP_FN(AdsPortOpenEx, long, (void))
TCMCP_FN(AdsPortCloseEx, long, (long))
TCMCP_FN(AdsGetLocalAddressEx, long, (long, PAmsAddr))
TCMCP_FN(AdsSyncReadReqEx2, long,
	 (long, PAmsAddr, unsigned long, unsigned long, unsigned long, void *, unsigned long *))
TCMCP_FN(AdsSyncWriteReqEx, long, (long, PAmsAddr, unsigned long, unsigned long, unsigned long, void *))
TCMCP_FN(AdsSyncReadWriteReqEx2, long,
	 (long, PAmsAddr, unsigned long, unsigned long, unsigned long, void *, unsigned long, void *,
	  unsigned long *))
TCMCP_FN(AdsSyncReadStateReqEx, long, (long, PAmsAddr, unsigned short *, unsigned short *))
TCMCP_FN(AdsSyncWriteControlReqEx, long,
	 (long, PAmsAddr, unsigned short, unsigned short, unsigned long, void *))
TCMCP_FN(AdsSyncReadDeviceInfoReqEx, long, (long, PAmsAddr, char *, PAdsVersion))
TCMCP_FN(AdsSyncAddDeviceNotificationReqEx, long,
	 (long, PAmsAddr, unsigned long, unsigned long, PAdsNotificationAttrib, PAdsNotificationFuncEx,
	  unsigned long, unsigned long *))
TCMCP_FN(AdsSyncDelDeviceNotificationReqEx, long, (long, PAmsAddr, unsigned long))
TCMCP_FN(AdsSyncSetTimeoutEx, long, (long, long))
TCMCP_FN(AdsSyncGetTimeoutEx, long, (long, long *))
#undef TCMCP_FN

const long NOT_LOADED = ADSERR_CLIENT_PORTNOTOPEN;

std::string env(const char *name)
{
	char buf[1024];
	DWORD n = GetEnvironmentVariableA(name, buf, sizeof(buf));
	return (n > 0 && n < sizeof(buf)) ? std::string(buf, n) : std::string();
}

bool resolve(char *err, unsigned long err_len)
{
#define TCMCP_GET(name)                                                                              \
	p_##name = reinterpret_cast<name##_t>(reinterpret_cast<void *>(GetProcAddress(g_dll, #name))); \
	if (!p_##name) {                                                                             \
		std::snprintf(err, err_len, "TcAdsDll.dll has no export %s (TwinCAT too old?)", #name); \
		return false;                                                                        \
	}
	TCMCP_GET(AdsPortOpenEx)
	TCMCP_GET(AdsPortCloseEx)
	TCMCP_GET(AdsGetLocalAddressEx)
	TCMCP_GET(AdsSyncReadReqEx2)
	TCMCP_GET(AdsSyncWriteReqEx)
	TCMCP_GET(AdsSyncReadWriteReqEx2)
	TCMCP_GET(AdsSyncReadStateReqEx)
	TCMCP_GET(AdsSyncWriteControlReqEx)
	TCMCP_GET(AdsSyncReadDeviceInfoReqEx)
	TCMCP_GET(AdsSyncAddDeviceNotificationReqEx)
	TCMCP_GET(AdsSyncDelDeviceNotificationReqEx)
	TCMCP_GET(AdsSyncSetTimeoutEx)
	TCMCP_GET(AdsSyncGetTimeoutEx)
#undef TCMCP_GET
	p_AdsGetDllVersion = reinterpret_cast<AdsGetDllVersion_t>(
		reinterpret_cast<void *>(GetProcAddress(g_dll, "AdsGetDllVersion")));
	return true;
}
} // namespace

bool tcmcp_load_tcadsdll(const char *explicit_path, char *err, unsigned long err_len, char *loaded_from,
			 unsigned long loaded_len)
{
	if (g_dll) {
		return true;
	}
	std::string candidates[4];
	int n = 0;
	if (explicit_path && *explicit_path) {
		candidates[n++] = explicit_path;
	}
	// TwinCAT 3.1: C:\TwinCAT\Common64\TcAdsDll.dll (TWINCAT3DIR = C:\TwinCAT\3.1\)
	std::string tc3 = env("TWINCAT3DIR");
	if (!tc3.empty()) {
		while (!tc3.empty() && (tc3.back() == '\\' || tc3.back() == '/')) {
			tc3.pop_back();
		}
		size_t cut = tc3.find_last_of("\\/");
		if (cut != std::string::npos) {
			candidates[n++] = tc3.substr(0, cut) + "\\Common64\\TcAdsDll.dll";
		}
	}
	candidates[n++] = "C:\\TwinCAT\\Common64\\TcAdsDll.dll";
	candidates[n++] = "TcAdsDll.dll"; // standard DLL search (PATH, System32)
	DWORD last = 0;
	for (int i = 0; i < n; ++i) {
		HMODULE h = LoadLibraryExA(candidates[i].c_str(), nullptr,
					   candidates[i].find('\\') != std::string::npos ? LOAD_WITH_ALTERED_SEARCH_PATH
										  : 0);
		if (h) {
			g_dll = h;
			char path[MAX_PATH] = { 0 };
			GetModuleFileNameA(h, path, MAX_PATH);
			std::snprintf(loaded_from, loaded_len, "%s", path);
			if (!resolve(err, err_len)) {
				FreeLibrary(h);
				g_dll = nullptr;
				return false;
			}
			return true;
		}
		last = GetLastError();
	}
	std::snprintf(err, err_len,
		      "TcAdsDll.dll not found (Windows error %lu). The TwinCAT ADS router must be installed "
		      "(TwinCAT 3 XAE/XAR or TC1000 ADS): looked in TWINCAT3DIR\\..\\Common64, C:\\TwinCAT\\Common64, PATH.",
		      static_cast<unsigned long>(last));
	return false;
}

extern "C" {
long TCADSDLL_API AdsGetDllVersion(void)
{
	return p_AdsGetDllVersion ? p_AdsGetDllVersion() : 0;
}
long TCADSDLL_API AdsPortOpenEx(void)
{
	return p_AdsPortOpenEx ? p_AdsPortOpenEx() : 0;
}
long TCADSDLL_API AdsPortCloseEx(long port)
{
	return p_AdsPortCloseEx ? p_AdsPortCloseEx(port) : NOT_LOADED;
}
long TCADSDLL_API AdsGetLocalAddressEx(long port, PAmsAddr pAddr)
{
	return p_AdsGetLocalAddressEx ? p_AdsGetLocalAddressEx(port, pAddr) : NOT_LOADED;
}
long TCADSDLL_API AdsSyncReadReqEx2(long port, PAmsAddr pAddr, unsigned long g, unsigned long o, unsigned long n,
				    void *d, unsigned long *r)
{
	return p_AdsSyncReadReqEx2 ? p_AdsSyncReadReqEx2(port, pAddr, g, o, n, d, r) : NOT_LOADED;
}
long TCADSDLL_API AdsSyncWriteReqEx(long port, PAmsAddr pAddr, unsigned long g, unsigned long o, unsigned long n,
				    void *d)
{
	return p_AdsSyncWriteReqEx ? p_AdsSyncWriteReqEx(port, pAddr, g, o, n, d) : NOT_LOADED;
}
long TCADSDLL_API AdsSyncReadWriteReqEx2(long port, PAmsAddr pAddr, unsigned long g, unsigned long o,
					 unsigned long rn, void *rd, unsigned long wn, void *wd, unsigned long *r)
{
	return p_AdsSyncReadWriteReqEx2 ? p_AdsSyncReadWriteReqEx2(port, pAddr, g, o, rn, rd, wn, wd, r) : NOT_LOADED;
}
long TCADSDLL_API AdsSyncReadStateReqEx(long port, PAmsAddr pAddr, unsigned short *a, unsigned short *d)
{
	return p_AdsSyncReadStateReqEx ? p_AdsSyncReadStateReqEx(port, pAddr, a, d) : NOT_LOADED;
}
long TCADSDLL_API AdsSyncWriteControlReqEx(long port, PAmsAddr pAddr, unsigned short a, unsigned short d,
					   unsigned long n, void *p)
{
	return p_AdsSyncWriteControlReqEx ? p_AdsSyncWriteControlReqEx(port, pAddr, a, d, n, p) : NOT_LOADED;
}
long TCADSDLL_API AdsSyncReadDeviceInfoReqEx(long port, PAmsAddr pAddr, char *name, PAdsVersion v)
{
	return p_AdsSyncReadDeviceInfoReqEx ? p_AdsSyncReadDeviceInfoReqEx(port, pAddr, name, v) : NOT_LOADED;
}
long TCADSDLL_API AdsSyncAddDeviceNotificationReqEx(long port, PAmsAddr pAddr, unsigned long g, unsigned long o,
						    PAdsNotificationAttrib a, PAdsNotificationFuncEx f,
						    unsigned long u, unsigned long *h)
{
	return p_AdsSyncAddDeviceNotificationReqEx ? p_AdsSyncAddDeviceNotificationReqEx(port, pAddr, g, o, a, f, u, h)
						   : NOT_LOADED;
}
long TCADSDLL_API AdsSyncDelDeviceNotificationReqEx(long port, PAmsAddr pAddr, unsigned long h)
{
	return p_AdsSyncDelDeviceNotificationReqEx ? p_AdsSyncDelDeviceNotificationReqEx(port, pAddr, h) : NOT_LOADED;
}
long TCADSDLL_API AdsSyncSetTimeoutEx(long port, long ms)
{
	return p_AdsSyncSetTimeoutEx ? p_AdsSyncSetTimeoutEx(port, ms) : NOT_LOADED;
}
long TCADSDLL_API AdsSyncGetTimeoutEx(long port, long *ms)
{
	return p_AdsSyncGetTimeoutEx ? p_AdsSyncGetTimeoutEx(port, ms) : NOT_LOADED;
}
}
#endif
