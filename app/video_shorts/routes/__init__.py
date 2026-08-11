from app.video_shorts import video_shorts_bp  # noqa: F401

# Import route modules so their decorators register with the blueprint
from . import auth  # noqa: F401
from . import channels  # noqa: F401
from . import videos  # noqa: F401
from . import generation  # noqa: F401
from . import quick_short  # noqa: F401
from . import api  # noqa: F401
from . import media  # noqa: F401
from . import settings  # noqa: F401
from . import stats  # noqa: F401
from . import dashboard_v2  # noqa: F401
from . import video_analytics  # noqa: F401
from . import home  # noqa: F401
from . import blog  # noqa: F401
from . import image_to_video  # noqa: F401
from . import monthly_top_video  # noqa: F401
from . import knowledge_base  # noqa: F401
from . import help  # noqa: F401
from . import ai_video  # noqa: F401
from . import interview  # noqa: F401
from . import billing  # noqa: F401
