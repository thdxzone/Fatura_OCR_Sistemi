"""İşletim sistemine göre süreç bellek ölçümü; destek yoksa değer uydurulmaz."""
import sys


def peak_rss_bytes():
    """macOS/Linux tepe belleğini bayta çevirir; Windows'ta şimdilik null döner."""
    try:
        import resource
    except ImportError:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == 'darwin' else value * 1024
