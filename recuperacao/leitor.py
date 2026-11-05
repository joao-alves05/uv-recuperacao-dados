# -*- coding: utf-8 -*-
"""Leitor de origem só leitura: arquivo comum, disco bruto ou volume, pela mesma interface.

    with abrir(caminho) as l:
        l.tamanho, l.setor, l.ler(offset, n)

Este módulo nunca pede acesso de escrita: no Windows o acesso é só GENERIC_READ (compartilhando leitura e escrita
com o resto do sistema); no Linux e para arquivos, só os.O_RDONLY. Em disco, a leitura é alinhada ao setor (o
leitor alarga o pedido ao setor e devolve só o que foi pedido).
"""
import os
import re
import stat

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
ERRO_ARQUIVO_NAO_ENCONTRADO = 2
ERRO_CAMINHO_NAO_ENCONTRADO = 3
ERRO_ACESSO_NEGADO = 5
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
IOCTL_DISK_GET_DRIVE_GEOMETRY = 0x00070000
IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
BLKSSZGET = 0x1268
BLKGETSIZE64 = 0x80081272

MSG_ADMIN = 'Para ler o disco inteiro, abra o programa como administrador'
SETOR_ARQUIVO = 512
_DISCO_WINDOWS = re.compile(r'\\\\\.\\(?:PhysicalDrive\d+|[A-Za-z]:)', re.I)
_OPTICO_WINDOWS = re.compile(r'\\\\\.\\CdRom\d+', re.I)
_DISPOSITIVO_LINUX = re.compile(r'/dev/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*')
_OPTICO_LINUX = re.compile(r'/dev/(?:sr|scd)\d+')
O_BINARY = getattr(os, 'O_BINARY', 0)


class ErroLeitor(OSError):
    """Falha ao abrir a origem; a mensagem já vem em português para o painel."""


class ErroPermissao(ErroLeitor):
    pass


def eh_dispositivo(caminho):
    """Caminho no formato de disco/volume (Windows \\\\.\\PhysicalDriveN ou \\\\.\\X:, Linux /dev/...). Não toca no disco."""
    if not isinstance(caminho, str) or any(p in ('.', '..') for p in caminho.split('/')):
        return False                                      # /dev/../etc/passwd não é dispositivo
    return bool(_DISCO_WINDOWS.fullmatch(caminho) or _OPTICO_WINDOWS.fullmatch(caminho)
                or _DISPOSITIVO_LINUX.fullmatch(caminho))


def eh_optico(caminho):
    return isinstance(caminho, str) and bool(_OPTICO_WINDOWS.fullmatch(caminho) or _OPTICO_LINUX.fullmatch(caminho))


def eh_administrador():
    try:
        if os.name == 'nt':
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False


class Leitor:
    """Base: valida o pedido, alinha ao setor quando preciso e corta no fim. Quem herda implementa `_bruto`."""
    alinhar = False

    def __init__(self, tamanho, setor):
        self.tamanho = tamanho
        self.setor = setor

    def _bruto(self, offset, n):
        raise NotImplementedError

    def ler(self, offset, n):
        if offset < 0 or n < 0:
            raise ValueError('offset e tamanho não podem ser negativos')
        n = min(n, max(0, self.tamanho - offset))
        if n == 0:
            return b''
        if not self.alinhar:
            return self._ler_completo(offset, n)
        s = self.setor
        ini = offset // s * s
        fim = -(-(offset + n) // s) * s
        bloco = self._ler_completo(ini, fim - ini, minimo=offset + n - ini)
        return bloco[offset - ini: offset - ini + n]

    def _ler_completo(self, offset, n, minimo=None):
        partes, lido = [], 0
        minimo = n if minimo is None else minimo
        while lido < n:
            b = self._bruto(offset + lido, n - lido)
            if not b:
                break
            partes.append(b)
            lido += len(b)
        if lido < minimo:
            raise OSError('leitura curta em %d (pedido %d, veio %d)' % (offset, n, lido))
        return b''.join(partes)

    def fechar(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.fechar()


class LeitorPosix(Leitor):
    def __init__(self, caminho):
        try:
            self.fd = os.open(caminho, os.O_RDONLY | O_BINARY)
        except PermissionError:
            raise ErroPermissao(MSG_ADMIN) from None
        except FileNotFoundError:
            raise ErroLeitor('Não encontrei a origem: %s' % caminho) from None
        except OSError as e:
            raise ErroLeitor('Não consegui abrir a origem para leitura: %s' % e.strerror) from None
        try:
            modo = os.fstat(self.fd).st_mode
            bloco = stat.S_ISBLK(modo)
            tamanho = os.lseek(self.fd, 0, os.SEEK_END) if (bloco or not stat.S_ISREG(modo)) else os.fstat(self.fd).st_size
            setor = SETOR_ARQUIVO
            if bloco:
                import fcntl
                import struct
                if tamanho <= 0:
                    tamanho = struct.unpack('Q', fcntl.ioctl(self.fd, BLKGETSIZE64, b'\0' * 8))[0]
                setor = struct.unpack('I', fcntl.ioctl(self.fd, BLKSSZGET, b'\0' * 4))[0] or SETOR_ARQUIVO
            self.alinhar = bloco
            super().__init__(tamanho, setor)
        except BaseException:
            os.close(self.fd)
            raise

    def _bruto(self, offset, n):
        os.lseek(self.fd, offset, os.SEEK_SET)
        return os.read(self.fd, n)

    def fechar(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class ApiWindows:
    """As únicas chamadas ao Windows: abrir só para leitura, medir, ler e fechar. Os testes trocam por uma falsa."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        self.k = k
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        k.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k.DeviceIoControl.restype = wintypes.BOOL
        k.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
        k.SetFilePointerEx.restype = wintypes.BOOL
        k.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                               ctypes.c_void_p]
        k.ReadFile.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.invalido = wintypes.HANDLE(-1).value

    def _erro(self):
        return OSError(None, 'erro do Windows', None, self.ct.get_last_error())

    def CreateFileW(self, caminho, acesso, compartilha, disposicao, flags):
        h = self.k.CreateFileW(caminho, acesso, compartilha, None, disposicao, flags, None)
        if h is None or h == self.invalido:
            raise self._erro()
        return h

    def _ioctl(self, h, codigo, saida):
        devolvidos = self.wt.DWORD(0)
        if not self.k.DeviceIoControl(h, codigo, None, 0, self.ct.byref(saida), self.ct.sizeof(saida),
                                      self.ct.byref(devolvidos), None):
            raise self._erro()

    def tamanho(self, h):
        saida = self.ct.c_longlong(0)
        self._ioctl(h, IOCTL_DISK_GET_LENGTH_INFO, saida)
        return saida.value

    def setor(self, h):
        class Geometria(self.ct.Structure):
            _fields_ = [('Cylinders', self.ct.c_longlong), ('MediaType', self.wt.DWORD), ('TracksPerCylinder', self.wt.DWORD),
                        ('SectorsPerTrack', self.wt.DWORD), ('BytesPerSector', self.wt.DWORD)]

        class GeometriaEx(self.ct.Structure):
            _fields_ = [('Geometry', Geometria), ('DiskSize', self.ct.c_longlong), ('Dados', self.ct.c_ubyte * 256)]
        try:
            ex = GeometriaEx()
            self._ioctl(h, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, ex)
            return ex.Geometry.BytesPerSector
        except OSError:
            g = Geometria()
            self._ioctl(h, IOCTL_DISK_GET_DRIVE_GEOMETRY, g)
            return g.BytesPerSector

    def extensoes(self, h):
        """Números dos discos físicos onde o volume aberto em `h` tem dados (IOCTL só de consulta)."""
        import struct
        buf = (self.ct.c_ubyte * (8 + 24 * 32))()
        self._ioctl(h, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, buf)
        dados = bytes(buf)
        n = struct.unpack_from('<I', dados, 0)[0]
        return [struct.unpack_from('<I', dados, 8 + 24 * i)[0] for i in range(min(n, 32))]

    def seek_penalty(self, h):
        """True se o dispositivo tem penalidade de busca (HD), False se não (SSD): IOCTL_STORAGE_QUERY_PROPERTY, só consulta."""
        class Consulta(self.ct.Structure):
            _fields_ = [('PropertyId', self.wt.DWORD), ('QueryType', self.wt.DWORD), ('Extra', self.ct.c_ubyte * 1)]
        entrada = Consulta(7, 0)                            # StorageDeviceSeekPenaltyProperty, PropertyStandardQuery
        saida = (self.ct.c_ubyte * 16)()
        devolvidos = self.wt.DWORD(0)
        if not self.k.DeviceIoControl(h, IOCTL_STORAGE_QUERY_PROPERTY, self.ct.byref(entrada), self.ct.sizeof(entrada),
                                      self.ct.byref(saida), self.ct.sizeof(saida), self.ct.byref(devolvidos), None):
            raise self._erro()
        return bool(saida[8])                               # DEVICE_SEEK_PENALTY_DESCRIPTOR.IncursSeekPenalty

    def ler(self, h, offset, n):
        if not self.k.SetFilePointerEx(h, offset, None, 0):
            raise self._erro()
        buf = self.ct.create_string_buffer(n)
        lido = self.wt.DWORD(0)
        if not self.k.ReadFile(h, buf, n, self.ct.byref(lido), None):
            raise self._erro()
        return buf.raw[:lido.value]

    def fechar(self, h):
        self.k.CloseHandle(h)


class LeitorWindows(Leitor):
    alinhar = True

    def __init__(self, caminho, api=None):
        self.api = api or ApiWindows()
        try:
            self.h = self.api.CreateFileW(caminho, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, OPEN_EXISTING, 0)
        except OSError as e:
            codigo = getattr(e, 'winerror', None) or e.errno
            if codigo == ERRO_ACESSO_NEGADO:
                raise ErroPermissao(MSG_ADMIN) from None
            if codigo in (ERRO_ARQUIVO_NAO_ENCONTRADO, ERRO_CAMINHO_NAO_ENCONTRADO):
                raise ErroLeitor('Não encontrei o disco %s. Ele foi removido?' % caminho) from None
            raise ErroLeitor('Não consegui abrir o disco para leitura (erro %s).' % codigo) from None
        try:
            tamanho = self.api.tamanho(self.h)
            try:
                setor = self.api.setor(self.h) or SETOR_ARQUIVO
            except OSError:
                setor = SETOR_ARQUIVO
            super().__init__(tamanho, setor)
        except BaseException as e:
            self.api.fechar(self.h)
            if isinstance(e, OSError):
                raise ErroLeitor('Não consegui medir o disco (erro %s).' % (getattr(e, 'winerror', None) or e.errno)) from None
            raise

    def _bruto(self, offset, n):
        return self.api.ler(self.h, offset, n)

    def fechar(self):
        if self.h is not None:
            self.api.fechar(self.h)
            self.h = None


def seek_penalty_do_disco(indice, api=None):
    """True se o disco físico N tem penalidade de busca (HD), False se não (SSD). Abre com acesso ZERO, só para perguntar."""
    api = api or ApiWindows()
    h = api.CreateFileW('\\\\.\\PhysicalDrive%d' % indice, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, OPEN_EXISTING, 0)
    try:
        return api.seek_penalty(h)
    finally:
        api.fechar(h)


def abrir(caminho, api=None):
    """Abre a origem só para leitura. Caminho de disco do Windows usa a API do Windows; o resto, os.open."""
    if os.name == 'nt' and (api is not None or _DISCO_WINDOWS.fullmatch(caminho) or _OPTICO_WINDOWS.fullmatch(caminho)):
        return LeitorWindows(caminho, api)
    if api is not None:
        return LeitorWindows(caminho, api)
    return LeitorPosix(caminho)
