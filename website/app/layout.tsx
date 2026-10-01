import type { Metadata, Viewport } from 'next';
import { Poppins, Space_Mono } from 'next/font/google';
import type { ReactNode } from 'react';

import './globals.css';

const poppins = Poppins({ subsets: ['latin'], weight: ['400', '500', '600', '700', '800'], variable: '--font-poppins', display: 'swap' });
const spaceMono = Space_Mono({ subsets: ['latin'], weight: ['400', '700'], variable: '--font-space-mono', display: 'swap' });

export const metadata: Metadata = {
  // Set NEXT_PUBLIC_SITE_URL to your domain so social previews use absolute image URLs.
  metadataBase: new URL(process.env.NEXT_PUBLIC_SITE_URL ?? 'https://agribot-site.onrender.com'),
  title: 'AgriBot — The field robot that spots crop disease first',
  description: 'AgriBot drives your rows, spots tomato leaf disease with on-board AI, probes the soil and tells you what to do — before the problem spreads.',
  openGraph: {
    title: 'AgriBot — smart farming robot',
    description: 'On-board crop-disease vision, soil sensors and instant alerts on your phone.',
    images: ['/screens/02-field.jpg'],
  },
};

export const viewport: Viewport = { themeColor: '#f1f3ee' };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${poppins.variable} ${spaceMono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
