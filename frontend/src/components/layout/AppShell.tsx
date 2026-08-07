import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';
import Header from './Header';
import BackgroundFX from '../ui/BackgroundFX';

export default function AppShell() {
  return (
    <div className="flex h-screen overflow-hidden bg-transparent">
      <BackgroundFX />
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden min-w-0">
        <Header />
        <main className="flex-1 overflow-auto px-5 py-5 md:px-7 md:py-6">
          <div className="mx-auto max-w-[1400px]">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
