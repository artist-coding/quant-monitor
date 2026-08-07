import { Component, type ErrorInfo, type ReactNode } from 'react';
import Card from './Card';
import Button from './Button';
import { IconAlert, IconRefresh } from './icons';

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  message: string;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, message: '' };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, message: error.message };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('ErrorBoundary caught:', error, info);
  }

  handleReload = () => {
    this.setState({ hasError: false, message: '' });
    window.location.reload();
  };

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex items-center justify-center h-96">
          <Card className="max-w-md w-full">
            <div className="flex flex-col items-center text-center py-8">
              <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-accent-red/10 ring-1 ring-inset ring-accent-red/25 text-accent-red mb-4">
                <IconAlert size={22} />
              </div>
              <div className="text-sm font-semibold text-text-primary mb-1">页面渲染出错</div>
              <div className="text-xs text-text-muted mb-5">{this.state.message || '未知错误'}</div>
              <Button variant="secondary" onClick={this.handleReload}>
                <IconRefresh size={14} />
                重新加载
              </Button>
            </div>
          </Card>
        </div>
      );
    }
    return this.props.children;
  }
}
