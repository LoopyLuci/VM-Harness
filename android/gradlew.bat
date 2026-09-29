@echo off
rem Gradle wrapper for Windows. Uses JAVA_HOME when it points at a JDK, otherwise the java on PATH (JDK 17+ needed).
setlocal
set "DIR=%~dp0"
if not defined GRADLE_USER_HOME set "GRADLE_USER_HOME=%LOCALAPPDATA%\Gradle"
set "JAVA_EXE=java.exe"
if defined JAVA_HOME if exist "%JAVA_HOME%\bin\java.exe" set "JAVA_EXE=%JAVA_HOME%\bin\java.exe"
"%JAVA_EXE%" -version >nul 2>&1
if errorlevel 1 (
    echo ERROR: no Java found. Install a JDK 17 or newer and set JAVA_HOME, or put java on PATH. 1>&2
    exit /b 1
)
"%JAVA_EXE%" -classpath "%DIR%gradle\wrapper\gradle-wrapper.jar" org.gradle.wrapper.GradleWrapperMain %*
exit /b %ERRORLEVEL%
