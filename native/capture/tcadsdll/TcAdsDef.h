// tcmcp-capture: declarations of the TcAdsDll data types, for building against the TwinCAT ADS router
// (TcAdsDll.dll) WITHOUT the TwinCAT SDK headers on the build machine (GitHub Actions has no TwinCAT).
//
// This is NOT Beckhoff's TcAdsDef.h. It declares only what Beckhoff's open-source AdsLib (TwinCAT mode,
// USE_TWINCAT_ROUTER) and tcmcp-capture use, with the layout documented by Beckhoff:
//   https://infosys.beckhoff.com/  ->  TwinCAT 3 / ADS DLL C++ / Datatypes
// The layout is checked by static_asserts in src/source_ads.cpp; building with the real SDK headers
// (cmake -DTCMCP_TCADSDLL_SDK=...) uses Beckhoff's own header instead and runs the same asserts.
#pragma once

#include <cstddef>

#pragma pack(push, 1)

typedef struct AmsNetId_ {
	unsigned char b[6];
} AmsNetId, *PAmsNetId;

typedef struct {
	AmsNetId netId;
	unsigned short port;
} AmsAddr, *PAmsAddr;

typedef struct {
	unsigned char version;
	unsigned char revision;
	unsigned short build;
} AdsVersion, *PAdsVersion;

typedef enum nAdsTransMode {
	ADSTRANS_NOTRANS = 0,
	ADSTRANS_CLIENTCYCLE = 1,
	ADSTRANS_CLIENTONCHA = 2,
	ADSTRANS_SERVERCYCLE = 3,
	ADSTRANS_SERVERONCHA = 4,
	ADSTRANS_SERVERCYCLE2 = 5,
	ADSTRANS_SERVERONCHA2 = 6,
	ADSTRANS_CLIENT1REQ = 10,
	ADSTRANS_MAXMODES
} ADSTRANSMODE;

typedef enum nAdsState {
	ADSSTATE_INVALID = 0,
	ADSSTATE_IDLE = 1,
	ADSSTATE_RESET = 2,
	ADSSTATE_INIT = 3,
	ADSSTATE_START = 4,
	ADSSTATE_RUN = 5,
	ADSSTATE_STOP = 6,
	ADSSTATE_SAVECFG = 7,
	ADSSTATE_LOADCFG = 8,
	ADSSTATE_POWERFAILURE = 9,
	ADSSTATE_POWERGOOD = 10,
	ADSSTATE_ERROR = 11,
	ADSSTATE_SHUTDOWN = 12,
	ADSSTATE_SUSPEND = 13,
	ADSSTATE_RESUME = 14,
	ADSSTATE_CONFIG = 15,
	ADSSTATE_RECONFIG = 16,
	ADSSTATE_STOPPING = 17,
	ADSSTATE_INCOMPATIBLE = 18,
	ADSSTATE_EXCEPTION = 19,
	ADSSTATE_MAXSTATES
} ADSSTATE;

typedef struct {
	unsigned long cbLength;
	ADSTRANSMODE nTransMode;
	unsigned long nMaxDelay;
	union {
		unsigned long nCycleTime;
		unsigned long dwChangeFilter;
	};
} AdsNotificationAttrib, *PAdsNotificationAttrib;

typedef struct {
	unsigned long hNotification;
	long long nTimeStamp;      // FILETIME: 100 ns since 1601-01-01 UTC, taken by the TwinCAT runtime
	unsigned long cbSampleSize;
	unsigned char data[1];     // cbSampleSize bytes
} AdsNotificationHeader, *PAdsNotificationHeader;

#pragma pack(pop)

#ifndef TCADSDLL_API
#define TCADSDLL_API __stdcall
#endif

// called by the ADS router for every sample (one call per sample, also when the router batched them)
typedef void(TCADSDLL_API *PAdsNotificationFuncEx)(AmsAddr *pAddr, AdsNotificationHeader *pNotification,
						    unsigned long hUser);

// reserved index groups (subset)
#define ADSIGRP_SYMTAB 0xF000
#define ADSIGRP_SYMNAME 0xF001
#define ADSIGRP_SYMVAL 0xF002
#define ADSIGRP_SYM_HNDBYNAME 0xF003
#define ADSIGRP_SYM_VALBYNAME 0xF004
#define ADSIGRP_SYM_VALBYHND 0xF005
#define ADSIGRP_SYM_RELEASEHND 0xF006
#define ADSIGRP_SYM_INFOBYNAME 0xF007
#define ADSIGRP_SYM_VERSION 0xF008
#define ADSIGRP_SYM_INFOBYNAMEEX 0xF009
#define ADSIGRP_SYM_UPLOAD 0xF00B
#define ADSIGRP_SYM_UPLOADINFO 0xF00C
#define ADSIGRP_SYM_DT_UPLOAD 0xF00E
#define ADSIGRP_SYM_UPLOADINFO2 0xF00F
#define ADSIGRP_SUMUP_READ 0xF080
#define ADSIGRP_SUMUP_WRITE 0xF081
#define ADSIGRP_SUMUP_READWRITE 0xF082
#define ADSIGRP_SUMUP_READEX 0xF083
#define ADSIGRP_SUMUP_READEX2 0xF084
#define ADSIGRP_SUMUP_ADDDEVNOTE 0xF085
#define ADSIGRP_SUMUP_DELDEVNOTE 0xF086
#define ADSIGRP_DEVICE_DATA 0xF100
#define ADSIOFFS_DEVDATA_ADSSTATE 0x0000
#define ADSIOFFS_DEVDATA_DEVSTATE 0x0002

// return codes (subset)
#define ADSERR_NOERR 0x00
#define ERR_ADSERRS 0x0700
#define ADSERR_DEVICE_ERROR (0x00 + ERR_ADSERRS)
#define ADSERR_DEVICE_SRVNOTSUPP (0x01 + ERR_ADSERRS)
#define ADSERR_DEVICE_INVALIDGRP (0x02 + ERR_ADSERRS)
#define ADSERR_DEVICE_INVALIDOFFSET (0x03 + ERR_ADSERRS)
#define ADSERR_DEVICE_INVALIDACCESS (0x04 + ERR_ADSERRS)
#define ADSERR_DEVICE_INVALIDSIZE (0x05 + ERR_ADSERRS)
#define ADSERR_DEVICE_INVALIDDATA (0x06 + ERR_ADSERRS)
#define ADSERR_DEVICE_NOTREADY (0x07 + ERR_ADSERRS)
#define ADSERR_DEVICE_BUSY (0x08 + ERR_ADSERRS)
#define ADSERR_DEVICE_NOMEMORY (0x0A + ERR_ADSERRS)
#define ADSERR_DEVICE_INVALIDPARM (0x0B + ERR_ADSERRS)
#define ADSERR_DEVICE_NOTFOUND (0x0C + ERR_ADSERRS)
#define ADSERR_DEVICE_SYMBOLNOTFOUND (0x10 + ERR_ADSERRS)
#define ADSERR_DEVICE_INVALIDSTATE (0x12 + ERR_ADSERRS)
#define ADSERR_DEVICE_TRANSMODENOTSUPP (0x13 + ERR_ADSERRS)
#define ADSERR_DEVICE_NOTIFYHNDINVALID (0x14 + ERR_ADSERRS)
#define ADSERR_DEVICE_NOMOREHDLS (0x16 + ERR_ADSERRS)
#define ADSERR_DEVICE_INVALIDWATCHSIZE (0x17 + ERR_ADSERRS)
#define ADSERR_DEVICE_TIMEOUT (0x19 + ERR_ADSERRS)
#define ADSERR_CLIENT_ERROR (0x40 + ERR_ADSERRS)
#define ADSERR_CLIENT_INVALIDPARM (0x41 + ERR_ADSERRS)
#define ADSERR_CLIENT_SYNCTIMEOUT (0x45 + ERR_ADSERRS)
#define ADSERR_CLIENT_W32ERROR (0x46 + ERR_ADSERRS)
#define ADSERR_CLIENT_PORTNOTOPEN (0x48 + ERR_ADSERRS)
#define ADSERR_CLIENT_NOAMSADDR (0x49 + ERR_ADSERRS)
