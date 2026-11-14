"""Windows process containment for HQ workers/stages.
Create suspended, assign to an exclusive KILL_ON_JOB_CLOSE Job Object, then resume.
Closing/crashing the owner kills its child tree. No taskkill, broad process scan,
registry/service changes or termination of unrelated processes.
"""
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import subprocess
import time
import uuid


class IO_COUNTERS(C.Structure):
    _fields_=[(x,C.c_ulonglong) for x in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
class BASIC_LIMIT(C.Structure):
    _fields_=[('PerProcessUserTimeLimit',C.c_longlong),('PerJobUserTimeLimit',C.c_longlong),('LimitFlags',W.DWORD),('MinimumWorkingSetSize',C.c_size_t),('MaximumWorkingSetSize',C.c_size_t),('ActiveProcessLimit',W.DWORD),('Affinity',C.c_size_t),('PriorityClass',W.DWORD),('SchedulingClass',W.DWORD)]
class EXTENDED_LIMIT(C.Structure):
    _fields_=[('BasicLimitInformation',BASIC_LIMIT),('IoInfo',IO_COUNTERS),('ProcessMemoryLimit',C.c_size_t),('JobMemoryLimit',C.c_size_t),('PeakProcessMemoryUsed',C.c_size_t),('PeakJobMemoryUsed',C.c_size_t)]
class ACCOUNTING(C.Structure):
    _fields_=[('TotalUserTime',C.c_longlong),('TotalKernelTime',C.c_longlong),('ThisPeriodTotalUserTime',C.c_longlong),('ThisPeriodTotalKernelTime',C.c_longlong),('TotalPageFaultCount',W.DWORD),('TotalProcesses',W.DWORD),('ActiveProcesses',W.DWORD),('TotalTerminatedProcesses',W.DWORD)]
class STARTUPINFO(C.Structure):
    _fields_=[('cb',W.DWORD),('lpReserved',W.LPWSTR),('lpDesktop',W.LPWSTR),('lpTitle',W.LPWSTR),('dwX',W.DWORD),('dwY',W.DWORD),('dwXSize',W.DWORD),('dwYSize',W.DWORD),('dwXCountChars',W.DWORD),('dwYCountChars',W.DWORD),('dwFillAttribute',W.DWORD),('dwFlags',W.DWORD),('wShowWindow',W.WORD),('cbReserved2',W.WORD),('lpReserved2',C.POINTER(C.c_ubyte)),('hStdInput',W.HANDLE),('hStdOutput',W.HANDLE),('hStdError',W.HANDLE)]
class PROCESS_INFORMATION(C.Structure):
    _fields_=[('hProcess',W.HANDLE),('hThread',W.HANDLE),('dwProcessId',W.DWORD),('dwThreadId',W.DWORD)]


def _api():
    k=C.WinDLL('kernel32',use_last_error=True)
    types={
        'CreateJobObjectW':([C.c_void_p,W.LPCWSTR],W.HANDLE),
        'SetInformationJobObject':([W.HANDLE,C.c_int,C.c_void_p,W.DWORD],W.BOOL),
        'AssignProcessToJobObject':([W.HANDLE,W.HANDLE],W.BOOL),
        'QueryInformationJobObject':([W.HANDLE,C.c_int,C.c_void_p,W.DWORD,C.c_void_p],W.BOOL),
        'TerminateJobObject':([W.HANDLE,W.UINT],W.BOOL),
        'CreateProcessW':([W.LPCWSTR,W.LPWSTR,C.c_void_p,C.c_void_p,W.BOOL,W.DWORD,C.c_void_p,W.LPCWSTR,C.POINTER(STARTUPINFO),C.POINTER(PROCESS_INFORMATION)],W.BOOL),
        'ResumeThread':([W.HANDLE],W.DWORD),
        'WaitForSingleObject':([W.HANDLE,W.DWORD],W.DWORD),
        'GetExitCodeProcess':([W.HANDLE,C.POINTER(W.DWORD)],W.BOOL),
        'TerminateProcess':([W.HANDLE,W.UINT],W.BOOL),
        'OpenProcess':([W.DWORD,W.BOOL,W.DWORD],W.HANDLE),
        'CloseHandle':([W.HANDLE],W.BOOL),
    }
    for name,(args,result) in types.items():
        f=getattr(k,name);f.argtypes=args;f.restype=result
    return k


def in_contained_tree(root_pid):
    """Verify both the venv launcher and this interpreter belong to our exact Job Object."""
    name=os.environ.get('HQ_CONTAINMENT_JOB','')
    if os.name!='nt' or not name.startswith('Local\\HQCover-'):return False
    k=_api()
    k.OpenJobObjectW.argtypes=[W.DWORD,W.BOOL,W.LPCWSTR];k.OpenJobObjectW.restype=W.HANDLE
    k.IsProcessInJob.argtypes=[W.HANDLE,W.HANDLE,C.POINTER(W.BOOL)];k.IsProcessInJob.restype=W.BOOL
    handle=k.OpenJobObjectW(4,False,name) # JOB_OBJECT_QUERY only
    if not handle:return False
    try:
        for pid in {int(root_pid or 0),os.getpid()}:
            if pid<=0:return False
            proc=k.OpenProcess(0x1000,False,pid) # PROCESS_QUERY_LIMITED_INFORMATION
            if not proc:return False
            try:
                found=W.BOOL()
                if not k.IsProcessInJob(proc,handle,C.byref(found)) or not found.value:return False
            finally:k.CloseHandle(proc)
        return True
    finally:k.CloseHandle(handle)


def run_contained(cmd,cwd,env,log_path,timeout_seconds,on_started=None):
    """Blocking managed process, preserving output in a new log; no shell.
    Returns {pid,returncode,timed_out,tree_empty,elapsed_seconds}.
    Parent must treat any nonzero or timed_out result as failure, never a ready artifact.
    """
    if os.name!='nt':raise RuntimeError('HQ process containment requires Windows')
    if not 0<timeout_seconds<=24*3600:raise ValueError('Invalid process timeout')
    import msvcrt
    k=_api();job_name='Local\\HQCover-'+uuid.uuid4().hex
    job=k.CreateJobObjectW(None,job_name)
    env=dict(env,HQ_CONTAINMENT_JOB=job_name)
    if not job:raise C.WinError(C.get_last_error())
    info=EXTENDED_LIMIT();info.BasicLimitInformation.LimitFlags=0x2000
    pi=PROCESS_INFORMATION();created=False;assigned=False;started=time.monotonic()
    try:
        if not k.SetInformationJobObject(job,9,C.byref(info),C.sizeof(info)):
            raise C.WinError(C.get_last_error())
        with Path(log_path).open('xb') as log,open(os.devnull,'rb') as null:
            out_handle=msvcrt.get_osfhandle(log.fileno());in_handle=msvcrt.get_osfhandle(null.fileno())
            os.set_handle_inheritable(out_handle,True);os.set_handle_inheritable(in_handle,True)
            si=STARTUPINFO();si.cb=C.sizeof(si);si.dwFlags=0x100
            si.hStdInput=in_handle;si.hStdOutput=out_handle;si.hStdError=out_handle
            command=C.create_unicode_buffer(subprocess.list2cmdline([str(x) for x in cmd]))
            environment=C.create_unicode_buffer('\0'.join(f'{key}={value}' for key,value in sorted(env.items(),key=lambda x:x[0].upper()))+'\0\0')
            flags=0x4|0x400|0x08000000
            ok=k.CreateProcessW(str(cmd[0]),command,None,None,True,flags,environment,str(cwd),C.byref(si),C.byref(pi))
            os.set_handle_inheritable(out_handle,False);os.set_handle_inheritable(in_handle,False)
            if not ok:raise C.WinError(C.get_last_error())
            created=True
            if not k.AssignProcessToJobObject(job,pi.hProcess):
                raise C.WinError(C.get_last_error())
            assigned=True
            if on_started:on_started(int(pi.dwProcessId))
            if k.ResumeThread(pi.hThread)==0xFFFFFFFF:raise C.WinError(C.get_last_error())
            k.CloseHandle(pi.hThread);pi.hThread=None
            wait=k.WaitForSingleObject(pi.hProcess,int(timeout_seconds*1000))
            timed_out=wait==0x102
            if wait==0xFFFFFFFF:raise C.WinError(C.get_last_error())
            if timed_out:
                if not k.TerminateJobObject(job,124):raise C.WinError(C.get_last_error())
                k.WaitForSingleObject(pi.hProcess,30000)
            code=W.DWORD()
            if not k.GetExitCodeProcess(pi.hProcess,C.byref(code)):raise C.WinError(C.get_last_error())
            # Hold descendant handles until their termination is signaled. The accounting
            # count can reach zero just before inherited file handles finish closing.
            class PID_LIST(C.Structure):
                _fields_=[('NumberOfAssignedProcesses',W.DWORD),('NumberOfProcessIdsInList',W.DWORD),('ProcessIdList',C.c_size_t*4096)]
            listing=PID_LIST();descendant_handles=[]
            if k.QueryInformationJobObject(job,3,C.byref(listing),C.sizeof(listing),None):
                for pid in listing.ProcessIdList[:listing.NumberOfProcessIdsInList]:
                    handle=k.OpenProcess(0x00100000,False,int(pid))
                    if handle:descendant_handles.append(handle)
            # The direct process may exit before its descendants. No unowned tree survives return.
            try:
                if not k.TerminateJobObject(job,125):raise C.WinError(C.get_last_error())
                for handle in descendant_handles:
                    if k.WaitForSingleObject(handle,30000)!=0:raise RuntimeError('Contained descendant did not exit')
            finally:
                for handle in descendant_handles:k.CloseHandle(handle)
            empty=False;deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                accounting=ACCOUNTING()
                if not k.QueryInformationJobObject(job,1,C.byref(accounting),C.sizeof(accounting),None):
                    raise C.WinError(C.get_last_error())
                if accounting.ActiveProcesses==0:empty=True;break
                time.sleep(.02)
            if not empty:raise RuntimeError('Contained process tree did not exit; do not release/retry task')
            return {'pid':int(pi.dwProcessId),'returncode':int(code.value),'timed_out':timed_out,'tree_empty':empty,'elapsed_seconds':time.monotonic()-started}
    finally:
        if created:
            if assigned:k.TerminateJobObject(job,125)
            else:k.TerminateProcess(pi.hProcess,125)
            k.WaitForSingleObject(pi.hProcess,30000)
            if pi.hThread:k.CloseHandle(pi.hThread)
            k.CloseHandle(pi.hProcess)
        k.CloseHandle(job)
