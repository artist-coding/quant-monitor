import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';
import Header from './Header';

export default function AppShell() {
  return (
    <div className="relative flex h-screen overflow-hidden bg-transparent">
      <div className="terminal-grid pointer-events-none absolute inset-0 z-0" />
      <Sidebar />
      <div className="relative z-10 flex min-w-0 flex-1 flex-col overflow-hidden">
        <Header />
        <main className="flex-1 overflow-auto">
          <div className="mx-auto w-full max-w-[1680px] px-4 py-5 md:px-6 md:py-6 xl:px-8">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
