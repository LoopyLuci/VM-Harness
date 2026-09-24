; VM-Harness Installer — NSIS script
; Builds VM-Harness-Installer.exe

!define PRODUCT_NAME "VM-Harness"
!define PRODUCT_VERSION "2.0.0"
!define PRODUCT_PUBLISHER "VM-Harness Team"
!define PRODUCT_WEB_SITE "https://github.com/LoopyLuci/VM-Harness"
!define PRODUCT_DIR_REGKEY "Software\Microsoft\Windows\CurrentVersion\App Paths\VM-Harness.exe"
!define PRODUCT_UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"
!define PRODUCT_UNINST_ROOT_KEY "HKLM"
!define PRODUCT_STARTMENU_REGKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"
!define MUTEX_NAME "Global\VM-Harness-GUI-SingleInstance"

; MUI 1.67 compatible ------
!include "MUI.nsh"
!include "LogicLib.nsh"

; MUI Settings
!define MUI_ABORTWARNING
!define MUI_ICON "vm-harness.ico"
!define MUI_UNICON "vm-harness.ico"
!define MUI_HEADERIMAGE
; !define MUI_HEADERIMAGE_BITMAP "vm-header.bmp"
!define MUI_HEADERIMAGE_RIGHT
; !define MUI_WELCOMEFINISHPAGE_BITMAP "vm-welcome.bmp"

; Welcome page
!insertmacro MUI_PAGE_WELCOME

; Feature List — 34 Panels + 2 Dialogs + Plugin API
; 
; VM Panels: VM Console, VM Control, VM Switcher, Multi-VM Dashboard
; Hypervisor: QEMU, QMP Console, VMware/VirtualBox
; Container: Container, Container Stats, Container Terminal, Container Logs
; K8s: K8s Tree, K8s Editor
; System: CPU Control, Display, Guest Agent, Guest Terminal, USB Device
; Storage: ISO Manager, Snapshot, Storage
; Network: Network, Security
; Monitor: Logs, Monitoring, Telemetry, SysInfo, Audit Log
; Management: Settings, Troubleshoot, Automation, Chat
; Integration: Pairing, AI Providers, Dashboard
; 
; Dialogs (2):
;   Container Logs Dialog, Image Pull Dialog
;
; Plugin API:
;   Extensible plugin system for custom integrations

; Components page
!define MUI_COMPONENTSPAGE_TEXT_DESCRIPTION "VM-Harness v2.0.0 — 34 panels, 2 dialogs, Plugin API. Full VM, container, Kubernetes, and hypervisor management suite."
!insertmacro MUI_PAGE_COMPONENTS
; Directory page
!insertmacro MUI_PAGE_DIRECTORY
; Instfiles page
!insertmacro MUI_PAGE_INSTFILES
; Finish page
!define MUI_FINISHPAGE_RUN "$INSTDIR\VM-Harness.exe"
!insertmacro MUI_PAGE_FINISH

; Uninstaller pages
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

; Language files
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "French"
!insertmacro MUI_LANGUAGE "German"
!insertmacro MUI_LANGUAGE "Spanish"
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "TradChinese"

; MUI end ------

Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
OutFile "VM-Harness-Installer.exe"
InstallDir "$PROGRAMFILES64\VM-Harness"
InstallDirRegKey HKLM "${PRODUCT_DIR_REGKEY}" ""
ShowInstDetails show
ShowUnInstDetails show
RequestExecutionLevel admin

; Check for running instance
Function .onInit
  ; Check if already running
  System::Call 'kernel32::CreateMutexW(i 0, i 0, t "${MUTEX_NAME}") i .r0'
  System::Call 'kernel32::GetLastError() i .r1'
  ${If} $r1 == 183  ; ERROR_ALREADY_EXISTS
    MessageBox MB_OK|MB_ICONEXCLAMATION "VM-Harness is already running. Please close it before installing."
    Abort
  ${EndIf}

  ; Extract the mutex handle for cleanup
  StrCpy $R0 $r0
FunctionEnd

Section "!VM-Harness Application" SEC01
  SectionIn RO
  SetOutPath "$INSTDIR"
  SetOverwrite ifnewer

  ; Main executable
  File /r "dist\VM-Harness\*.*"

  ; Icon for shortcuts
  File "vm-harness.ico"

  ; Create uninstaller
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; Registry entries
  WriteRegStr HKLM "${PRODUCT_DIR_REGKEY}" "" "$INSTDIR\VM-Harness.exe"
  WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "DisplayName" "${PRODUCT_NAME}"
  WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "DisplayIcon" "$INSTDIR\vm-harness.ico"
  WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "DisplayVersion" "${PRODUCT_VERSION}"
  WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "InfoAbout" "${PRODUCT_WEB_SITE}"
  WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "Publisher" "${PRODUCT_PUBLISHER}"
  WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "URLInfoAbout" "${PRODUCT_WEB_SITE}"

  ; Start Menu shortcuts
  CreateDirectory "$SMPROGRAMS\VM-Harness"
  CreateShortCut "$SMPROGRAMS\VM-Harness\VM-Harness.lnk" "$INSTDIR\VM-Harness.exe" "" "$INSTDIR\vm-harness.ico" 0
  CreateShortCut "$SMPROGRAMS\VM-Harness\Uninstall.lnk" "$INSTDIR\Uninstall.exe" "" "$INSTDIR\vm-harness.ico" 0

  ; Desktop shortcut
  CreateShortCut "$DESKTOP\VM-Harness.lnk" "$INSTDIR\VM-Harness.exe" "" "$INSTDIR\vm-harness.ico" 0
SectionEnd

Section "Start Menu Shortcuts" SEC02
  CreateDirectory "$SMPROGRAMS\VM-Harness"
  CreateShortCut "$SMPROGRAMS\VM-Harness\VM-Harness.lnk" "$INSTDIR\VM-Harness.exe" "" "$INSTDIR\vm-harness.ico" 0
  CreateShortCut "$SMPROGRAMS\VM-Harness\Uninstall.lnk" "$INSTDIR\Uninstall.exe" "" "$INSTDIR\vm-harness.ico" 0
SectionEnd

Section "Desktop Shortcut" SEC03
  CreateShortCut "$DESKTOP\VM-Harness.lnk" "$INSTDIR\VM-Harness.exe" "" "$INSTDIR\vm-harness.ico" 0
SectionEnd

Section -AdditionalIcons
  CreateShortCut "$SMPROGRAMS\VM-Harness\Website.lnk" "${PRODUCT_WEB_SITE}" "" "$INSTDIR\vm-harness.ico" 0
SectionEnd

Section -Post
  WriteRegStr HKLM "${PRODUCT_DIR_REGKEY}" "" "$INSTDIR\VM-Harness.exe"
  WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "DisplayName" "${PRODUCT_NAME}"
  WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "DisplayIcon" "$INSTDIR\vm-harness.ico"
  WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "DisplayVersion" "${PRODUCT_VERSION}"
  WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "URLInfoAbout" "${PRODUCT_WEB_SITE}"
  WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "Publisher" "${PRODUCT_PUBLISHER}"
  WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "InfoAbout" "${PRODUCT_WEB_SITE}"

  ; Release the installer mutex (not the app mutex)
  System::Call 'kernel32::CloseHandle(i $R0)'
SectionEnd

Section Uninstall
  ; Remove from Start Menu
  Delete "$SMPROGRAMS\VM-Harness\VM-Harness.lnk"
  Delete "$SMPROGRAMS\VM-Harness\Uninstall.lnk"
  Delete "$SMPROGRAMS\VM-Harness\Website.lnk"
  RMDir "$SMPROGRAMS\VM-Harness"

  ; Remove Desktop shortcut
  Delete "$DESKTOP\VM-Harness.lnk"

  ; Remove installed files
  Delete "$INSTDIR\VM-Harness.exe"
  Delete "$INSTDIR\Uninstall.exe"
  Delete "$INSTDIR\vm-harness.ico"

  ; Remove registry
  DeleteRegKey HKLM "${PRODUCT_UNINST_KEY}"
  DeleteRegKey HKLM "${PRODUCT_DIR_REGKEY}"

  RMDir "$INSTDIR"
SectionEnd

Function un.onInit
  MessageBox MB_ICONQUESTION|MB_YESNO|MB_DEFBUTTON2 "Are you sure you want to completely remove ${PRODUCT_NAME} and all of its components?" IDYES +2
  Abort
FunctionEnd
