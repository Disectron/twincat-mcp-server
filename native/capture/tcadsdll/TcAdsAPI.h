// tcmcp-capture: declarations of the TcAdsDll functions (see TcAdsDef.h for why this file exists).
// The functions are resolved at run time from Beckhoff's TcAdsDll.dll (src/tcadsdll_loader.cpp), so the
// executable needs no import library and starts with a clear message when TwinCAT is not installed.
// Signatures as documented in Beckhoff InfoSys, "ADS DLL C++ -> Functions" (x64: one calling convention).
#pragma once

#include "TcAdsDef.h"

extern "C" {
long TCADSDLL_API AdsGetDllVersion(void);
long TCADSDLL_API AdsPortOpenEx(void);
long TCADSDLL_API AdsPortCloseEx(long port);
long TCADSDLL_API AdsGetLocalAddressEx(long port, PAmsAddr pAddr);
long TCADSDLL_API AdsSyncReadReqEx2(long port, PAmsAddr pAddr, unsigned long nIndexGroup, unsigned long nIndexOffset,
				    unsigned long nLength, void *pData, unsigned long *pcbReturn);
long TCADSDLL_API AdsSyncWriteReqEx(long port, PAmsAddr pAddr, unsigned long nIndexGroup, unsigned long nIndexOffset,
				    unsigned long nLength, void *pData);
long TCADSDLL_API AdsSyncReadWriteReqEx2(long port, PAmsAddr pAddr, unsigned long nIndexGroup,
					 unsigned long nIndexOffset, unsigned long nReadLength, void *pReadData,
					 unsigned long nWriteLength, void *pWriteData, unsigned long *pcbReturn);
long TCADSDLL_API AdsSyncReadStateReqEx(long port, PAmsAddr pAddr, unsigned short *pAdsState,
					unsigned short *pDeviceState);
long TCADSDLL_API AdsSyncWriteControlReqEx(long port, PAmsAddr pAddr, unsigned short nAdsState,
					   unsigned short nDeviceState, unsigned long nLength, void *pData);
long TCADSDLL_API AdsSyncReadDeviceInfoReqEx(long port, PAmsAddr pAddr, char *pDevName, PAdsVersion pVersion);
long TCADSDLL_API AdsSyncAddDeviceNotificationReqEx(long port, PAmsAddr pAddr, unsigned long nIndexGroup,
						    unsigned long nIndexOffset, PAdsNotificationAttrib pNoteAttrib,
						    PAdsNotificationFuncEx pNoteFunc, unsigned long hUser,
						    unsigned long *pNotification);
long TCADSDLL_API AdsSyncDelDeviceNotificationReqEx(long port, PAmsAddr pAddr, unsigned long hNotification);
long TCADSDLL_API AdsSyncSetTimeoutEx(long port, long nMs);
long TCADSDLL_API AdsSyncGetTimeoutEx(long port, long *pnMs);
}

// tcmcp-capture: load TcAdsDll.dll and resolve the functions above. Returns false with a message when the
// DLL or an export is missing. `explicit_path` (may be empty) is tried first.
bool tcmcp_load_tcadsdll(const char *explicit_path, char *err, unsigned long err_len, char *loaded_from,
			 unsigned long loaded_len);
