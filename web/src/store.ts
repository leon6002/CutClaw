/** 全局状态(zustand)— 架构升级 2026-07-11:
 *  - useNavStore: 当前 tab 与 URL hash 双向同步(刷新不再回主页)
 *  - useImmichStore: 相簿/相簿内容缓存(秒开 + 后台静默刷新)+ 当前相簿
 *    持久化到 sessionStorage(刷新恢复现场) */
import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";
import { api } from "./api";

// ── 导航:tab ↔ location.hash ─────────────────────────────────────────────
const tabFromHash = () => {
  const t = window.location.hash.replace(/^#\/?/, "").split(/[/?]/)[0];
  return ["assets", "editor", "render", "immich"].includes(t) ? t : "assets";
};

export const useNavStore = create<{
  tab: string;
  setTab: (t: string) => void;
  syncFromHash: () => void;
}>((set) => ({
  tab: tabFromHash(),
  setTab: (t) => {
    if (window.location.hash !== `#/${t}`) window.location.hash = `/${t}`;
    set({ tab: t });
  },
  syncFromHash: () => set({ tab: tabFromHash() }),
}));

window.addEventListener("hashchange", () => useNavStore.getState().syncFromHash());

// ── Immich 管理:缓存 + 现场恢复 ─────────────────────────────────────────
export type ImAlbum = { id: string; name: string; count: number; thumb?: string; start?: string; end?: string };
export type ImItem = {
  id: string; name: string; type: string; taken_at: string; thumb: string;
  duration?: any; rating?: number; favorite?: boolean; has_gps?: boolean;
  city?: string; geo_done?: boolean; score_done?: boolean; size_mb?: number;
};

type ImmichState = {
  albums: ImAlbum[];
  albumsAt: number;
  itemsByAlbum: Record<string, { items: ImItem[]; at: number }>;
  currentAlbumId: string | null;
  loadAlbums: (force?: boolean) => Promise<void>;
  openAlbum: (id: string | null) => void;
  loadItems: (id: string, force?: boolean) => Promise<void>;
};

const TTL = 5 * 60_000;   // 缓存视为新鲜的窗口;过期仍先展示旧数据再后台刷新

export const useImmichStore = create<ImmichState>()(
  persist(
    (set, get) => ({
      albums: [],
      albumsAt: 0,
      itemsByAlbum: {},
      currentAlbumId: null,

      loadAlbums: async (force = false) => {
        const { albums, albumsAt } = get();
        if (!force && albums.length && Date.now() - albumsAt < TTL) return;
        try {
          const r = await api<{ albums: ImAlbum[] }>("/api/immich/albums");
          set({ albums: r.albums ?? [], albumsAt: Date.now() });
        } catch { /* 保留旧缓存 */ }
      },

      openAlbum: (id) => set({ currentAlbumId: id }),

      loadItems: async (id, force = false) => {
        const cached = get().itemsByAlbum[id];
        if (!force && cached && Date.now() - cached.at < TTL) return;
        try {
          const r = await api<{ items: ImItem[] }>(`/api/immich/mgmt/album/${id}`);
          set((s) => ({ itemsByAlbum: { ...s.itemsByAlbum, [id]: { items: r.items ?? [], at: Date.now() } } }));
        } catch { /* 保留旧缓存 */ }
      },
    }),
    {
      name: "cutclaw-immich",
      storage: createJSONStorage(() => sessionStorage),
      // 只持久化轻量字段:相簿列表 + 当前相簿 id;资产列表太大留在内存
      partialize: (s) => ({ albums: s.albums, albumsAt: s.albumsAt, currentAlbumId: s.currentAlbumId }),
    },
  ),
);
