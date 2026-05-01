from calibre.customize import InterfaceActionBase


class AIMetadataReviewPlugin(InterfaceActionBase):
    name = 'AI Metadata Review'
    description = 'Review selected books, normalize authors, and optionally ask OpenAI for fixes.'
    supported_platforms = ['windows', 'osx', 'linux']
    author = 'Naz'
    version = (0, 1, 0)
    minimum_calibre_version = (8, 0, 0)
    actual_plugin = 'calibre_plugins.ai_metadata_review.ui:MetadataReviewAction'

    def is_customizable(self):
        return True

    def config_widget(self):
        from calibre_plugins.ai_metadata_review.config import ConfigWidget
        return ConfigWidget()

    def save_settings(self, config_widget):
        config_widget.save_settings()
        ac = self.actual_plugin_
        if ac is not None and hasattr(ac, 'apply_settings'):
            ac.apply_settings()

